"""
main.py
=======
Central loop driver: a strict Finite State Machine wiring together broker,
data, strategy, and risk layers. One position lock — no concurrent entries.

Run directly:
    PAPER_MODE=true python /app/backend/main.py
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Local package imports
sys.path.insert(0, str(Path(__file__).parent))

import config
from broker.smartapi_client import SmartApiError, build_client
from broker.websocket_manager import HeartbeatLapse, OrderEvent, Tick, WebSocketManager
from data.candle_manager import Bar, CandleManager
from data.indicator_engine import IndicatorEngine, VixTracker
from data.option_selector import OptionContract, OptionSelector
from database.sqlite_logger import get_logger
from risk.liquidity_gate import LiquidityGate
from risk.pnl_guard import PnlGuard
from risk.position_sizer import PositionSizer
from strategy.confirmation_engine import ConfirmationEngine
from strategy.position_manager import PendingEntry, PositionManager
from strategy.regime_filter import RegimeFilter
from strategy.signal_generator import SignalGenerator

logger = logging.getLogger("nifty_bot")

# IST is UTC + 5:30. Container clock is UTC; we localise for the window checks.
IST_OFFSET = timedelta(hours=5, minutes=30)


def _now_ist() -> datetime:
    return datetime.now(timezone.utc) + IST_OFFSET


def _ist_time() -> dtime:
    return _now_ist().time()


# ────────────────────────────────────────────────────────────────────
# FSM driver
# ────────────────────────────────────────────────────────────────────
class NiftyOptionsBot:
    def __init__(self) -> None:
        self.db = get_logger()
        self.state: config.State = config.State.IDLE
        self._state_lock = threading.RLock()
        self._stop = threading.Event()

        self.broker = None
        self.ws: Optional[WebSocketManager] = None
        self.candles = CandleManager()
        self.indicators = IndicatorEngine(
            config.EMA_FAST, config.EMA_SLOW,
            config.EMA_MACRO_FAST, config.EMA_MACRO_SLOW,
            config.RSI_PERIOD, config.ATR_PERIOD,
        )
        self.vix = VixTracker()
        self.option_selector = OptionSelector()

        self.regime = RegimeFilter()
        self.signal_gen = SignalGenerator(config.EMA_FAST, config.EMA_SLOW)
        self.confirm = ConfirmationEngine()
        self.liquidity = LiquidityGate()
        self.positions = PositionManager()

        self.sizer: Optional[PositionSizer] = None
        self.pnl_guard: Optional[PnlGuard] = None
        self._effective_lots: int = config.MIN_LOTS

        # daily counters / breakers
        self._trades_today = 0
        self._consecutive_losses = 0
        self._api_reject_count = 0
        self._cooldown_until: Optional[datetime] = None

        # latest option contracts (CE/PE) and quote latches
        self._ce: Optional[OptionContract] = None
        self._pe: Optional[OptionContract] = None
        self._last_option_quote: dict[str, dict] = {}  # token -> {ltp,bid,ask,vol,oi}

        # entry-bar bookkeeping for confirmation
        self._pending_signal: Optional[dict] = None  # {'direction', 'bar_ts'}
        # Source ('auto' | 'manual') of the currently pending entry so the trade
        # record can be tagged on fill.
        self._pending_source: Optional[str] = None

    # ────────────────────────────────────────────────────────── lifecycle
    def start(self) -> None:
        _configure_logging()
        logger.info(
            "Booting NiftyOptionsBot  paper=%s  log=%s  db=%s",
            config.PAPER_MODE, config.LOG_LEVEL, config.DB_PATH,
        )

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        self.broker = build_client()

        # Morning init: drawdown-aware sizing
        capital = self.broker.get_net_available_cash()
        self.sizer = PositionSizer(self.db)
        sized = self.sizer.update_equity_and_size(current_equity=capital)
        self._effective_lots = sized.effective_lots
        self.pnl_guard = PnlGuard(sized.daily_loss_cap, sized.daily_profit_lock)
        logger.info(
            "Morning sizing → lots=%d (scale=%.2f, dd=%.2f%%) loss_cap=₹%.0f profit_lock=₹%.0f",
            sized.effective_lots, sized.scale_multiplier,
            sized.drawdown_pct * 100, sized.daily_loss_cap, sized.daily_profit_lock,
        )

        # Scrip master + ATM picks (only when using a live broker)
        if config.USE_LIVE_BROKER:
            try:
                self.option_selector.load()
                self._refresh_atm_contracts()
            except Exception:
                logger.exception("Scrip master / ATM picks failed; bot will idle.")

        # WebSocket layer
        token_subs = self._build_token_subscriptions()
        self.ws = WebSocketManager(
            feed_token=self.broker.get_feed_token(),
            client_id=config.ANGEL_CLIENT_ID,
            jwt=self.broker.get_jwt(),
            on_tick=self._on_tick,
            on_order=self._on_order,
            is_shutdown=lambda: self.state is config.State.SHUTDOWN,
            heartbeat_armed=lambda: self.positions.has_open_position,
            token_subscriptions=token_subs,
        )
        self.ws.start()

        # main loop
        try:
            self._main_loop()
        finally:
            self._shutdown()

    def _on_signal(self, *_args) -> None:
        logger.warning("Signal received — initiating graceful shutdown.")
        self._stop.set()

    def _build_token_subscriptions(self) -> list[dict[str, Any]]:
        """Compose the NSE_CM tokens (spot + VIX) the bot needs. ATM CE/PE on
        NSE_FO are added later once the option_selector has resolved them."""
        nse_cm_tokens: list[str] = []
        if config.NIFTY_SPOT_TOKEN:
            nse_cm_tokens.append(config.NIFTY_SPOT_TOKEN)
        if config.INDIA_VIX_TOKEN:
            nse_cm_tokens.append(config.INDIA_VIX_TOKEN)
        nse_fo_tokens: list[str] = []
        if self._ce:
            nse_fo_tokens.append(self._ce.token)
        if self._pe:
            nse_fo_tokens.append(self._pe.token)

        subs: list[dict[str, Any]] = []
        if nse_cm_tokens:
            subs.append({"exchangeType": 1, "tokens": nse_cm_tokens})  # 1 = NSE_CM
        if nse_fo_tokens:
            subs.append({"exchangeType": 2, "tokens": nse_fo_tokens})  # 2 = NSE_FO
        return subs

    def _refresh_atm_contracts(self) -> None:
        """Pick ATM CE/PE for the nearest expiry using current Nifty spot LTP."""
        try:
            spot = self.broker.ltp("NSE", "NIFTY", config.NIFTY_SPOT_TOKEN)
        except Exception:
            logger.exception("Spot LTP fetch failed; cannot pick ATM contracts.")
            return
        try:
            ce, pe = self.option_selector.select_atm(spot)
            self._ce = ce
            self._pe = pe
            logger.info(
                "ATM picks → CE=%s (token=%s), PE=%s (token=%s) [spot=%.2f]",
                ce.symbol, ce.token, pe.symbol, pe.token, spot,
            )
        except Exception:
            logger.exception("ATM contract resolution failed.")

    # ────────────────────────────────────────────────────── manual entries
    def _handle_manual_entry(self, direction: config.Direction) -> tuple[bool, str]:
        """Fire a discretionary entry. Enforces the same single-position lock,
        sizing, ATR-based SL/TP, OCO, trailing, and cooldown as auto entries."""
        # Hard guards — same single-position lock used by auto entries
        if self.state is config.State.SHUTDOWN:
            return False, "bot is in SHUTDOWN"
        if self.positions.has_open_position or self.positions.has_pending_entry:
            return False, "another position is already open/pending"
        if self.positions.in_cooldown(direction):
            return False, f"{direction.value} is in directional cooldown after a recent stop"
        breach = self._trip_circuit_breakers()
        if breach:
            return False, f"circuit breaker: {breach}"
        # Need an ATM contract picked
        contract = self._ce if direction is config.Direction.LONG else self._pe
        if contract is None:
            self._refresh_atm_contracts()
            contract = self._ce if direction is config.Direction.LONG else self._pe
        if contract is None:
            return False, "ATM contract not yet resolved"
        # ATR for SL/TP — fall back to 10 premium points if the option series
        # is too fresh to compute a real ATR(14).
        option_bars = self.candles.series(contract.token, 3).closed_bars()
        snap = self.indicators.build_snapshot(
            self.candles.series(config.NIFTY_SPOT_TOKEN, 3).closed_bars(),
            self.candles.series(config.NIFTY_SPOT_TOKEN, 15).closed_bars(),
            option_bars,
        )
        atr_pts = snap.atr_3m or 10.0
        sl_pts, tp_pts = self.sizer.stops_from_atr(atr_pts)
        ok = self._place_entry(direction, contract, sl_pts, tp_pts, source="manual")
        if ok:
            return True, f"manual {direction.value} placed with ATR={atr_pts:.2f}"
        return False, "broker rejected the entry"

    def _drain_command_queue(self) -> None:
        """Pull one pending command per loop tick and execute it. Manual entries
        share the same FSM rails, so the bot stays at the strict single-position lock."""
        cmd = self.db.fetch_pending_command()
        if not cmd:
            return
        cmd_id, action, payload = cmd
        try:
            import json
            data = json.loads(payload) if payload else {}
            if action == "manual_entry":
                d = (data.get("direction") or "").upper()
                direction = config.Direction.LONG if d == "CALL" else (
                    config.Direction.SHORT if d == "PUT" else None
                )
                if direction is None:
                    self.db.complete_command(cmd_id, False, "unknown direction")
                    return
                ok, msg = self._handle_manual_entry(direction)
                self.db.complete_command(cmd_id, ok, msg)
                logger.info("Manual command #%d → %s (%s)", cmd_id, "OK" if ok else "FAIL", msg)
            elif action == "panic_exit":
                if self.positions.has_open_position:
                    self._transition(config.State.FORCED_EXIT)
                    self.db.complete_command(cmd_id, True, "FORCED_EXIT triggered")
                else:
                    self.db.complete_command(cmd_id, False, "no open position to exit")
            else:
                self.db.complete_command(cmd_id, False, f"unknown action {action}")
        except Exception as exc:
            logger.exception("Command #%d failed", cmd_id)
            self.db.complete_command(cmd_id, False, str(exc))

    def _shutdown(self) -> None:
        try:
            if self.ws is not None:
                self.ws.stop()
            if self.broker is not None:
                self.broker.logout()
        finally:
            self.db.close()
            logger.info("Bot shut down.")

    # ────────────────────────────────────────────────────────── helpers
    def _transition(self, new_state: config.State) -> None:
        with self._state_lock:
            if new_state is self.state:
                return
            old = self.state.name
            self.state = new_state
            self.db.log_state_transition(old, new_state.name)
            logger.info("FSM %s → %s", old, new_state.name)

    def _in_entry_window(self) -> bool:
        t = _ist_time()
        return config.ENTRY_WINDOW_START <= t <= config.ENTRY_WINDOW_END

    def _past_square_off(self) -> bool:
        return _ist_time() >= config.INTRADAY_SQUARE_OFF

    def _vix_ok(self) -> bool:
        v = self.vix.value
        if v is None:
            # in PAPER mode VIX may never arrive; permit if undefined.
            return config.PAPER_MODE
        return config.VIX_MIN <= v <= config.VIX_MAX

    def _trip_circuit_breakers(self) -> Optional[str]:
        if self._trades_today >= config.MAX_TRADES_DAILY:
            return "max_trades_daily"
        if self._consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            return "max_consecutive_losses"
        if self._api_reject_count >= config.MAX_API_REJECT_EVENTS:
            return "max_api_rejects"
        if self.ws and self.ws.reconnect_failures >= config.MAX_WS_RECONNECT_FAILS:
            return "max_ws_reconnect_fails"
        if self.pnl_guard and self.pnl_guard.evaluate().breached:
            return self.pnl_guard.evaluate().reason
        return None

    # ────────────────────────────────────────────────────────── tick / order
    def _on_tick(self, t: Tick) -> None:
        # Route by token: spot, VIX, option contracts.
        if t.token == config.INDIA_VIX_TOKEN:
            self.vix.update(t.ltp)
            return
        if t.token == config.NIFTY_SPOT_TOKEN:
            self.candles.series(t.token, 3).ingest_tick(t.ltp, t.volume)
            self.candles.series(t.token, 15).ingest_tick(t.ltp, t.volume)
            self._update_live_state(spot=t.ltp)
            return
        # Option tick
        self._last_option_quote[t.token] = {
            "ltp": t.ltp, "bid": t.bid, "ask": t.ask, "volume": t.volume, "oi": t.oi,
        }
        self.candles.series(t.token, 3).ingest_tick(t.ltp, t.volume)
        # If this is the open position's contract, push live LTP into bot_state
        pos = self.positions.open_position
        if pos and pos.contract_token == t.token:
            self._update_live_state(option_ltp=t.ltp)

    def _update_live_state(self, spot: Optional[float] = None,
                            option_ltp: Optional[float] = None) -> None:
        try:
            import json
            current = self.db.get_state("live_quotes")
            payload = json.loads(current[0]) if current else {}
            if spot is not None:
                payload["spot"] = spot
            if option_ltp is not None:
                payload["option_ltp"] = option_ltp
            vix_val = self.vix.value
            if vix_val is not None:
                payload["vix"] = vix_val
            payload["ts"] = time.time()
            self.db.set_state("live_quotes", json.dumps(payload))
        except Exception:
            pass

    def _on_order(self, ev: OrderEvent) -> None:
        # Once terminal, ignore everything (prevents SHUTDOWN ↔ FORCED_EXIT loop)
        if self.state is config.State.SHUTDOWN:
            return
        if ev.status == "heartbeat_lapse":
            logger.critical("Heartbeat lapse — routing to FORCED_EXIT")
            self._transition(config.State.FORCED_EXIT)
            return
        if ev.status == "rejected":
            self._api_reject_count += 1
            logger.warning("Order rejected (#%d): %s", self._api_reject_count, ev.text)
            self.positions.clear_pending()
            self._transition(config.State.IDLE)
            return
        if ev.status == "complete":
            self._handle_fill(ev)

    def _handle_fill(self, ev: OrderEvent) -> None:
        pending = self.positions.pending_entry
        if pending and ev.order_id == pending.order_id:
            # Slippage check — wider tolerance for MARKET orders since some
            # slippage is the explicit trade-off for guaranteed fills.
            tol = (
                config.MARKET_FILL_TOLERANCE_PCT
                if config.ENTRY_ORDER_TYPE == "MARKET"
                else config.FILL_TOLERANCE_PCT
            )
            if ev.fill_price > pending.expected_price * (1 + tol):
                logger.warning(
                    "Fill %.2f exceeds %.0f%% tolerance vs ref %.2f — emergency exit.",
                    ev.fill_price, tol * 100, pending.expected_price,
                )
                self.positions.clear_pending()
                self._transition(config.State.FORCED_EXIT)
                return
            pos = self.positions.promote_to_open(ev.fill_price)
            source = self._pending_source if self._pending_source else "auto"
            self._pending_source = None
            self.db.insert_trade_entry(
                pos.trade_id, pos.direction.value, pos.qty, pos.entry_price,
                source=source,
            )
            self._place_protective_legs(pos.target_price, pos.stop_price)
            self._transition(config.State.POSITION_OPEN)
            return

        # Exit-leg fill
        pos = self.positions.open_position
        if pos and ev.order_id in (pos.target_order_id, pos.stop_order_id):
            self._finalize_exit(ev.fill_price, was_stop=ev.order_id == pos.stop_order_id)

    # ────────────────────────────────────────────────────────── order flow
    def _place_entry(
        self, direction: config.Direction, contract: OptionContract, sl_pts: float, tp_pts: float,
        source: str = "auto",
    ) -> bool:
        quote = self._last_option_quote.get(contract.token)
        if not quote and not config.SIMULATE_ORDERS:
            logger.info("No quote for %s yet; skipping.", contract.symbol)
            return False
        premium = (quote or {}).get("ltp", 100.0)

        # Premium-spike guard
        lots = self.sizer.premium_spike_guard(
            option_premium=premium,
            effective_lots=self._effective_lots,
            current_equity=self.broker.get_net_available_cash(),
        )
        qty = lots * config.LOT_SIZE_NIFTY

        # Build the entry payload — MARKET or LIMIT per config toggle
        is_market = config.ENTRY_ORDER_TYPE == "MARKET"
        if is_market:
            limit_px = premium  # reference for SL/TP draft + slippage check
            payload = {
                "variety": "NORMAL",
                "tradingsymbol": contract.symbol,
                "symboltoken": contract.token,
                "transactiontype": "BUY",
                "exchange": contract.exchange,
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "price": "0",
                "quantity": qty,
            }
        else:
            limit_px = premium * (1 + config.LIMIT_SLIP_BUFFER_PCT)
            payload = {
                "variety": "NORMAL",
                "tradingsymbol": contract.symbol,
                "symboltoken": contract.token,
                "transactiontype": "BUY",
                "exchange": contract.exchange,
                "ordertype": "LIMIT",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "price": round(limit_px, 2),
                "quantity": qty,
            }

        # Provisional SL/TP — will be re-anchored to actual fill in
        # PositionManager.promote_to_open()
        target_px = premium + tp_pts
        stop_px = max(0.05, premium - sl_pts)
        try:
            order_id = self.broker.place_order(payload)
        except SmartApiError as exc:
            self._api_reject_count += 1
            logger.warning("Broker rejected entry: %s", exc)
            return False

        self.positions.register_pending_entry(
            PendingEntry(
                order_id=order_id,
                direction=direction,
                contract_symbol=contract.symbol,
                contract_token=contract.token,
                expected_price=limit_px,
                lots=lots,
                qty=qty,
                target_price=target_px,
                stop_price=stop_px,
                sl_points=sl_pts,
                tp_points=tp_pts,
            )
        )
        self._pending_source = source
        self._transition(config.State.ORDER_PENDING)
        logger.info(
            "Entry sent [%s · %s]  %s  qty=%d  ref=₹%.2f  tgt=+%.1fpts  sl=-%.1fpts  order=%s",
            source.upper(), config.ENTRY_ORDER_TYPE, contract.symbol, qty,
            limit_px, tp_pts, sl_pts, order_id,
        )

        # PAPER / SIM mode: synthesise an immediate fill so the FSM advances
        if config.SIMULATE_ORDERS and self.ws is not None:
            self.ws.force_order(
                OrderEvent(order_id=order_id, status="complete",
                           fill_price=limit_px, avg_price=limit_px,
                           text="paper-fill", ts=time.time())
            )
        return True

    def _place_protective_legs(self, target_price: float, stop_price: float) -> None:
        pos = self.positions.open_position
        if pos is None:
            return
        # Target = LIMIT SELL  (user wants target ALWAYS as limit — locks in
        # the planned reward, no slippage on the upside).
        tgt = {
            "variety": "NORMAL",
            "tradingsymbol": pos.contract_symbol,
            "symboltoken": pos.contract_token,
            "transactiontype": "SELL",
            "exchange": "NFO",
            "ordertype": "LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": round(target_price, 2),
            "quantity": pos.qty,
        }
        # SL leg — STOPLOSS_MARKET (default) protects against gap-throughs.
        # When SL_ORDER_TYPE is STOPLOSS_LIMIT we fall back to a stop-limit
        # with a tight buffer.
        if config.SL_ORDER_TYPE == "STOPLOSS_MARKET":
            sl = {
                "variety": "NORMAL",
                "tradingsymbol": pos.contract_symbol,
                "symboltoken": pos.contract_token,
                "transactiontype": "SELL",
                "exchange": "NFO",
                "ordertype": "STOPLOSS_MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "price": "0",
                "triggerprice": round(stop_price, 2),
                "quantity": pos.qty,
            }
        else:
            sl = {
                **tgt,
                "ordertype": "STOPLOSS_LIMIT",
                "price": round(stop_price, 2),
                "triggerprice": round(stop_price * 1.001, 2),
            }
        try:
            tgt_id = self.broker.place_order(tgt)
            sl_id = self.broker.place_order(sl)
            self.positions.set_protective_orders(tgt_id, sl_id)
            logger.info(
                "Protective OCO armed [SL=%s]: tgt=%s sl=%s  (target ₹%.2f, stop ₹%.2f)",
                config.SL_ORDER_TYPE, tgt_id, sl_id, target_price, stop_price,
            )
        except SmartApiError as exc:
            logger.exception("Failed to arm OCO: %s", exc)
            self._transition(config.State.FORCED_EXIT)

    def _finalize_exit(self, exit_price: float, was_stop: bool) -> None:
        pos = self.positions.close_position(exit_was_stop=was_stop)
        if pos is None:
            return
        # cancel sibling leg
        other = pos.target_order_id if was_stop else pos.stop_order_id
        if other:
            try:
                self.broker.cancel_order(other)
            except Exception:
                pass

        pnl = (exit_price - pos.entry_price) * pos.qty
        reason = config.ExitReason.STOP_LOSS.value if was_stop else config.ExitReason.TARGET.value
        self.db.update_trade_exit(pos.trade_id, exit_price, pnl, reason)
        self.pnl_guard.add_realized(pnl)
        self._trades_today += 1
        self._consecutive_losses = self._consecutive_losses + 1 if pnl < 0 else 0
        logger.info(
            "Trade closed %s pnl=₹%.2f trades_today=%d consec_loss=%d",
            reason, pnl, self._trades_today, self._consecutive_losses,
        )
        self._enter_cooldown()

    def _enter_cooldown(self) -> None:
        self._cooldown_until = datetime.now(timezone.utc) + timedelta(
            minutes=config.COOLDOWN_AFTER_EXIT_MIN
        )
        self._transition(config.State.COOLDOWN)

    # ────────────────────────────────────────────────────────── FSM steps
    def _step_idle(self) -> None:
        if not self._in_entry_window() or not self._vix_ok():
            self._update_signal_diag("blocked: outside entry window or VIX")
            return
        if self.positions.has_open_position or self.positions.has_pending_entry:
            return

        bars_3m = self.candles.series(config.NIFTY_SPOT_TOKEN, 3).closed_bars()
        bars_15m = self.candles.series(config.NIFTY_SPOT_TOKEN, 15).closed_bars()
        snap = self.indicators.build_snapshot(bars_3m, bars_15m)
        if snap.ema9 is None or snap.ema20_15m is None:
            self._update_signal_diag(f"warming up · bars3m={len(bars_3m)} bars15m={len(bars_15m)}")
            return

        regime = self.regime.evaluate(snap)
        cross = self.signal_gen.latest_cross(bars_3m, snap)
        self._update_signal_diag(
            f"regime_long={regime.authorize_long} regime_short={regime.authorize_short} "
            f"cross={cross.direction.name if cross else 'none'}"
        )
        if cross is None:
            return

        if cross.direction is config.Direction.LONG and not regime.authorize_long:
            return
        if cross.direction is config.Direction.SHORT and not regime.authorize_short:
            return
        if self.positions.in_cooldown(cross.direction):
            return

        self._pending_signal = {
            "direction": cross.direction,
            "bar_ts": bars_3m[-1].ts if bars_3m else None,
        }
        self.db.log_indicator_snapshot(
            {
                "ema9": snap.ema9, "ema21": snap.ema21,
                "ema20_15m": snap.ema20_15m, "ema50_15m": snap.ema50_15m,
                "rsi": snap.rsi, "adx": snap.adx, "vwap": snap.vwap,
            }
        )
        self._transition(config.State.WAIT_CONFIRMATION)

    def _update_signal_diag(self, note: str) -> None:
        """Snapshot of why no signal is firing — surfaces in /api/bot/status."""
        try:
            import json
            bars_3m = self.candles.series(config.NIFTY_SPOT_TOKEN, 3).closed_bars()
            bars_15m = self.candles.series(config.NIFTY_SPOT_TOKEN, 15).closed_bars()
            snap = self.indicators.build_snapshot(bars_3m, bars_15m)
            self.db.set_state("signal_diag", json.dumps({
                "note": note,
                "bars_3m": len(bars_3m),
                "bars_15m": len(bars_15m),
                "rsi": snap.rsi,
                "adx": snap.adx,
                "adx_prev": snap.adx_prev,
                "adx_delta_req": config.ADX_DELTA_MIN,
                "adx_min_req": config.ADX_MIN,
                "rsi_long_req": config.RSI_LONG,
                "rsi_short_req": config.RSI_SHORT,
                "vix": self.vix.value,
                "vix_band": [config.VIX_MIN, config.VIX_MAX],
                "ema_macro_fast": snap.ema20_15m,
                "ema_macro_slow": snap.ema50_15m,
                "ema_fast_3m": snap.ema9,
                "ema_slow_3m": snap.ema21,
                "last_close": snap.last_close,
                "vwap": snap.vwap,
            }))
        except Exception:
            pass

    def _step_wait_confirmation(self) -> None:
        sig = self._pending_signal
        if not sig:
            self._transition(config.State.IDLE)
            return
        bars_3m = self.candles.series(config.NIFTY_SPOT_TOKEN, 3).closed_bars()
        bars_15m = self.candles.series(config.NIFTY_SPOT_TOKEN, 15).closed_bars()
        if not bars_3m or bars_3m[-1].ts == sig["bar_ts"]:
            return  # waiting for the next 3m bar to close

        # we now have a fresh follow-through bar
        contract = self._ce if sig["direction"] is config.Direction.LONG else self._pe
        if contract is None:
            logger.info("No ATM contract picked yet — skipping confirmation.")
            self._pending_signal = None
            self._transition(config.State.IDLE)
            return

        option_bars = self.candles.series(contract.token, 3).closed_bars()
        snap = self.indicators.build_snapshot(bars_3m, bars_15m, option_bars)
        ver = self.confirm.validate(sig["direction"], snap)
        if not ver.ok:
            logger.info("Confirmation FAIL %s → %s", sig["direction"].name, ver.reasons)
            self._pending_signal = None
            self._transition(config.State.IDLE)
            return

        q = self._last_option_quote.get(contract.token, {})
        liq = self.liquidity.check(
            bid=q.get("bid", 0.0), ask=q.get("ask", 0.0),
            volume=q.get("volume", 0), open_interest=q.get("oi", 0),
        )
        if not liq.ok and not config.SIMULATE_ORDERS:
            logger.info("Liquidity FAIL → %s", liq.reasons)
            self._pending_signal = None
            self._transition(config.State.IDLE)
            return

        atr_pts = snap.atr_3m or 5.0  # graceful default in early session
        sl_pts, tp_pts = self.sizer.stops_from_atr(atr_pts)
        ok = self._place_entry(sig["direction"], contract, sl_pts, tp_pts, source="auto")
        self._pending_signal = None
        if not ok:
            self._transition(config.State.IDLE)

    def _step_order_pending(self) -> None:
        p = self.positions.pending_entry
        if p is None:
            self._transition(config.State.IDLE)
            return
        if p.age_seconds() >= config.ORDER_TIMEOUT_SEC:
            logger.warning("Entry order %s unfilled in %ds — cancelling.",
                           p.order_id, config.ORDER_TIMEOUT_SEC)
            if p.order_id and p.order_id not in (None, "None", ""):
                try:
                    self.broker.cancel_order(p.order_id)
                except Exception:
                    logger.exception("cancel_order failed (continuing)")
            self.positions.clear_pending()
            self._transition(config.State.IDLE)

    def _step_position_open(self) -> None:
        pos = self.positions.open_position
        if pos is None:
            self._transition(config.State.IDLE)
            return

        # square-off & max-hold guards
        if self._past_square_off() or pos.age_seconds() >= config.MAX_HOLD_TIME_MIN * 60:
            self._transition(config.State.FORCED_EXIT)
            return

        # trail stop using latest premium
        q = self._last_option_quote.get(pos.contract_token, {})
        ltp = q.get("ltp")
        if ltp:
            new_stop = self.positions.maybe_trail_stop(ltp)
            if new_stop is not None and pos.stop_order_id:
                # cancel-replace stop leg
                try:
                    self.broker.cancel_order(pos.stop_order_id)
                    if config.SL_ORDER_TYPE == "STOPLOSS_MARKET":
                        new_sl_payload = {
                            "variety": "NORMAL",
                            "tradingsymbol": pos.contract_symbol,
                            "symboltoken": pos.contract_token,
                            "transactiontype": "SELL",
                            "exchange": "NFO",
                            "ordertype": "STOPLOSS_MARKET",
                            "producttype": "INTRADAY",
                            "duration": "DAY",
                            "price": "0",
                            "triggerprice": round(new_stop, 2),
                            "quantity": pos.qty,
                        }
                    else:
                        new_sl_payload = {
                            "variety": "NORMAL",
                            "tradingsymbol": pos.contract_symbol,
                            "symboltoken": pos.contract_token,
                            "transactiontype": "SELL",
                            "exchange": "NFO",
                            "ordertype": "STOPLOSS_LIMIT",
                            "producttype": "INTRADAY",
                            "duration": "DAY",
                            "price": round(new_stop, 2),
                            "triggerprice": round(new_stop * 1.001, 2),
                            "quantity": pos.qty,
                        }
                    new_id = self.broker.place_order(new_sl_payload)
                    self.positions.set_protective_orders(pos.target_order_id, new_id)
                    logger.info("Stop trailed → %.2f (%s order=%s)", new_stop, config.SL_ORDER_TYPE, new_id)
                except Exception:
                    logger.exception("Trail stop replace failed.")

    def _step_forced_exit(self) -> None:
        pos = self.positions.open_position
        if pos is None:
            self._enter_cooldown()
            return
        # purge protective legs and fire market sell
        for oid in (pos.target_order_id, pos.stop_order_id):
            if oid:
                try:
                    self.broker.cancel_order(oid)
                except Exception:
                    pass
        try:
            self.broker.place_order({
                "variety": "NORMAL",
                "tradingsymbol": pos.contract_symbol,
                "symboltoken": pos.contract_token,
                "transactiontype": "SELL",
                "exchange": "NFO",
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "quantity": pos.qty,
                "price": 0,
            })
        except Exception:
            logger.exception("Market flatten failed.")
        ltp = self._last_option_quote.get(pos.contract_token, {}).get("ltp", pos.entry_price)
        self._finalize_exit(ltp, was_stop=True)

    def _step_cooldown(self) -> None:
        breach = self._trip_circuit_breakers()
        if breach:
            logger.critical("Circuit breaker tripped: %s — SHUTDOWN", breach)
            self._transition(config.State.SHUTDOWN)
            return
        if self._cooldown_until and datetime.now(timezone.utc) >= self._cooldown_until:
            self._cooldown_until = None
            self._transition(config.State.IDLE)

    # ────────────────────────────────────────────────────────── main loop
    def _main_loop(self) -> None:
        TICK_INTERVAL = 0.5
        while not self._stop.is_set():
            try:
                self.broker.validate_session()

                # Pull at most one pending command per tick (manual entries,
                # panic exits, etc.) before stepping the FSM.
                self._drain_command_queue()

                breach = self._trip_circuit_breakers()
                if breach and self.state is not config.State.SHUTDOWN:
                    logger.critical("Circuit breaker: %s", breach)
                    if self.positions.has_open_position:
                        self._transition(config.State.FORCED_EXIT)
                    else:
                        self._transition(config.State.SHUTDOWN)

                s = self.state
                if s is config.State.IDLE:
                    self._step_idle()
                elif s is config.State.WAIT_CONFIRMATION:
                    self._step_wait_confirmation()
                elif s is config.State.ORDER_PENDING:
                    self._step_order_pending()
                elif s is config.State.POSITION_OPEN:
                    self._step_position_open()
                elif s is config.State.FORCED_EXIT:
                    self._step_forced_exit()
                elif s is config.State.COOLDOWN:
                    self._step_cooldown()
                elif s is config.State.SHUTDOWN:
                    logger.info("In SHUTDOWN — terminal loop.")
                    time.sleep(5)
                    if self._stop.is_set():
                        break

            except HeartbeatLapse:
                self._transition(config.State.FORCED_EXIT)
            except Exception:
                logger.exception("Main loop iteration crashed; continuing.")
            time.sleep(TICK_INTERVAL)


# ────────────────────────────────────────────────────────────────────
# Bootstrap
# ────────────────────────────────────────────────────────────────────
def _configure_logging() -> None:
    fmt = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO), format=fmt)


if __name__ == "__main__":
    NiftyOptionsBot().start()
