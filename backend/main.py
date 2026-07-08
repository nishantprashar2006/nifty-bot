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
from strategy.smc_engine import classify_strength as smc_classify, evaluate as smc_evaluate
from notifications.telegram import TelegramNotifier

logger = logging.getLogger("nifty_bot")

# IST is UTC + 5:30. Container clock is UTC; we localise for the window checks.
IST_OFFSET = timedelta(hours=5, minutes=30)


def _now_ist() -> datetime:
    return datetime.now(timezone.utc) + IST_OFFSET


def _ist_time() -> dtime:
    return _now_ist().time()


# SMC trade-grade mapping (user-specified): 95+ A+, 90+ A, 85+ B+, 80+ B,
# 75+ C, otherwise D. Independent of the indicator engine's strength bands.
def _smc_grade(confidence: int) -> str:
    if confidence >= 95: return "A+"
    if confidence >= 90: return "A"
    if confidence >= 85: return "B+"
    if confidence >= 80: return "B"
    if confidence >= 75: return "C"
    return "D"


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
        # PART 3 — engine ('indicator'|'smc'), confidence, and reasons that
        # triggered the manual entry. Persisted to the trades row on fill.
        self._pending_engine: Optional[str] = None
        self._pending_confidence: Optional[int] = None
        self._pending_reasons: list[str] = []

        # SMC signal freshness tracker — independent of indicator FSM.
        # Holds {'direction', 'confidence', 'grade', 'generated_at'} for the
        # current live SMC signal. Auto-expires after MAX_SIGNAL_AGE_MINUTES.
        self._smc_signal: Optional[dict] = None

        # P0-5: throttle for periodic atm_snapshot refresh (~every 10s).
        self._last_atm_publish_ts: float = 0.0

        # Explicit exit-reason hint for the next FORCED_EXIT / synthetic exit.
        # Whoever triggers the exit sets this so `_finalize_exit` records the
        # true cause (TIME_STOP / SQUARE_OFF / MANUAL / HEARTBEAT) instead
        # of flattening everything to STOP_LOSS.
        self._exit_reason_hint: Optional[str] = None

        # Rolling per-leg spread history for the liquidity penalty. Storing
        # the last 3 spread_pct readings per option token lets the penalty
        # calculation use the median instead of a single tick, so one stale
        # WebSocket quote can't zero the setup score on its own. Same
        # formula, same 50-point ceiling — only the input is smoothed.
        from collections import deque
        self._spread_history: dict[str, deque] = {}
        # Rolling per-token LTP history for the SMC synthetic SL/TP and
        # trailing-SL price reads (shared smoothing primitive with the
        # spread penalty). Same 3-tick median, same helper.
        self._ltp_history: dict[str, deque] = {}

        # Telegram notifier — advisory only; never touches trading logic.
        # Reads env config at construction and swallows any downstream error.
        self.telegram = TelegramNotifier()

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
        self.pnl_guard = PnlGuard(sized.daily_loss_cap)
        logger.info(
            "Morning sizing → lots=%d (scale=%.2f, dd=%.2f%%) loss_cap=₹%.0f "
            "(profit lock removed per P0-7)",
            sized.effective_lots, sized.scale_multiplier,
            sized.drawdown_pct * 100, sized.daily_loss_cap,
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

        # Boot-time orphan recovery: if a trade row exists with no exit_time,
        # mark it closed (bot lost in-memory state on restart). User can
        # re-open via Buy Call/Put manually.
        self._recover_orphan_trade()

        # One-off Telegram startup ping — proves the bot is alive and the
        # credentials are configured. No-op if TELEGRAM_ENABLED=false.
        try:
            self.telegram.send_startup()
        except Exception:
            logger.exception("Telegram startup ping raised (ignored)")

        # main loop
        try:
            self._main_loop()
        finally:
            self._shutdown()

    def _on_signal(self, *_args) -> None:
        logger.warning("Signal received — initiating graceful shutdown.")
        self._stop.set()

    def _recover_orphan_trade(self) -> None:
        """PART 3 §15 — Crash-recovery.

        On boot, check the DB for any trade row with no exit_time. If the
        broker still reports a matching open position, ADOPT it back into
        the FSM (re-attach to its SL/TP orders, resume trailing). Only
        when the broker is genuinely flat do we mark the DB row closed —
        this avoids the previous bug where a DB-close masked a live
        broker position.
        """
        try:
            import sqlite3
            from datetime import datetime as _dt, timezone as _tz
            con = sqlite3.connect(config.DB_PATH)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT trade_id, direction, entry_price, qty, entry_time, "
                "sl_price, tp_price FROM trades "
                "WHERE exit_time IS NULL ORDER BY entry_time DESC LIMIT 1"
            ).fetchone()
            self.db.log_state_transition("BOOT", "IDLE")
            if not row:
                con.close()
                return

            trade_id = row["trade_id"]
            # Query the broker for open positions
            try:
                broker_positions = self.broker.positions() if self.broker else []
            except Exception as exc:
                logger.critical(
                    "Boot recovery: broker.positions() failed (%s). "
                    "DB orphan %s left open — manual review required.",
                    exc, trade_id,
                )
                con.close()
                return

            # Filter to non-zero option positions
            open_opt = [
                p for p in broker_positions
                if str(p.get("exchange", "")).upper() == "NFO"
                and int(float(p.get("netqty") or 0)) != 0
            ]

            if not open_opt:
                # Broker is flat — safe to mark DB orphan closed
                con.execute(
                    "UPDATE trades SET exit_time=?, exit_price=?, pnl=?, "
                    "exit_reason=? WHERE trade_id=?",
                    (_dt.now(_tz.utc).isoformat(), row["entry_price"], 0.0,
                     "RESTART_ORPHAN_RECOVERY", trade_id),
                )
                con.commit()
                con.close()
                logger.warning(
                    "Boot recovery: DB orphan %s closed; broker is flat.",
                    trade_id,
                )
                return

            # Broker has an open option position → ADOPT it
            bp = open_opt[0]
            net = int(float(bp.get("netqty") or 0))
            entry_px = float(bp.get("avgnetprice") or bp.get("buyavgprice") or row["entry_price"])
            symbol = bp.get("tradingsymbol", "")
            token = str(bp.get("symboltoken", ""))
            direction = (
                config.Direction.LONG
                if "CE" in symbol.upper() else config.Direction.SHORT
            )

            # Look for resting SL/TP legs in the order book
            sl_id: Optional[str] = None
            tp_id: Optional[str] = None
            try:
                ob = self.broker.order_book()
            except Exception:
                ob = []
            for o in ob:
                if str(o.get("symboltoken")) != token:
                    continue
                status = str(o.get("status") or o.get("orderstatus") or "").lower()
                if status not in {"open", "trigger pending", "trigger_pending", "pending"}:
                    continue
                ot = str(o.get("ordertype") or "").upper()
                if "STOPLOSS" in ot or "SL" in ot:
                    sl_id = str(o.get("orderid"))
                elif ot == "LIMIT" and str(o.get("transactiontype", "")).upper() == "SELL":
                    tp_id = str(o.get("orderid"))

            stop_px = float(row["sl_price"] or entry_px * (1 - config.MANUAL_SL_PCT))
            target_px = float(row["tp_price"] or entry_px * (1 + config.MANUAL_TP_PCT))

            from strategy.position_manager import OpenPosition
            pos = OpenPosition(
                trade_id=trade_id,
                direction=direction,
                contract_symbol=symbol,
                contract_token=token,
                qty=abs(net),
                lots=max(1, abs(net) // config.LOT_SIZE_NIFTY),
                entry_price=entry_px,
                entry_ts=_dt.now(_tz.utc),
                target_price=target_px,
                stop_price=stop_px,
                target_order_id=tp_id,
                stop_order_id=sl_id,
                trail_anchor=entry_px,
                trail_step_pct=config.TRAIL_STEP_PCT,
            )
            self.positions.adopt_open_position(pos)
            self._transition(config.State.POSITION_OPEN)
            logger.critical(
                "Boot recovery: ADOPTED broker position %s qty=%d entry=₹%.2f "
                "(SL order=%s · TP order=%s) — resuming trailing.",
                symbol, abs(net), entry_px, sl_id, tp_id,
            )
            con.close()
        except Exception:
            logger.exception("orphan recovery failed (continuing in IDLE)")

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
        """Pick ATM CE/PE for the nearest expiry using current Nifty spot LTP.

        Also publishes an `atm_snapshot` row to bot_state so the dashboard's
        confirmation modal shows the *actual* strike/expiry/token/premium
        that would be sent to the broker — no more generic 'ATM weekly CE'
        placeholder text.
        """
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
            self._publish_atm_snapshot(spot)
        except Exception:
            logger.exception("ATM contract resolution failed.")

    def _publish_atm_snapshot(self, spot: float) -> None:
        """P0-5: expose the currently-resolved CE/PE picks + a best-effort
        premium (WS quote if we have one, else REST LTP) so the dashboard
        can display the exact contract in the confirmation dialog."""
        import json as _json
        try:
            def _leg(c: Optional[OptionContract]) -> Optional[dict]:
                if c is None:
                    return None
                q = self._last_option_quote.get(c.token, {})
                ltp = float(q.get("ltp") or 0.0)
                if ltp <= 0:
                    # One-shot REST LTP so the modal has a real number to show.
                    try:
                        ltp = float(self.broker.ltp(c.exchange, c.symbol, c.token) or 0.0)
                    except Exception:
                        ltp = 0.0
                return {
                    "symbol": c.symbol,
                    "token": c.token,
                    "strike": c.strike,
                    "expiry": c.expiry,
                    "option_type": c.option_type,
                    "lot_size": c.lot_size,
                    "exchange": c.exchange,
                    "ltp": round(ltp, 2) if ltp > 0 else None,
                }
            snap = {
                "spot": round(float(spot), 2),
                "ce": _leg(self._ce),
                "pe": _leg(self._pe),
                "ts": time.time(),
            }
            self.db.set_state("atm_snapshot", _json.dumps(snap))
        except Exception:
            logger.exception("Failed to publish atm_snapshot.")

    # ────────────────────────────────────────────────────── manual entries
    def _handle_manual_entry(
        self,
        direction: config.Direction,
        lots_override: Optional[int] = None,
        engine: str = "indicator",
        confidence: Optional[int] = None,
        reasons: Optional[list[str]] = None,
    ) -> tuple[bool, str]:
        """Fire a discretionary entry. PART 3 §6-8: manual SL = 15 % of fill,
        TP = 30 %, trailing step = 10 %. Same single-position lock, sizing
        guards, OCO, and cooldown as the auto path.

        P0-1: ALWAYS refresh the Near-OTM contract right before the order is
        built. Startup-cached _ce/_pe are never used for manual entries —
        the strike, expiry, and token that go to the broker are always the
        ones computed against the latest spot at the moment of execution.
        """
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
        # P0-1: mandatory pre-entry refresh. Never trust startup cache.
        try:
            self._refresh_atm_contracts()
        except Exception:
            logger.exception("Pre-entry ATM refresh failed; aborting manual entry.")
            return False, "ATM refresh failed — could not resolve current strike"
        contract = self._ce if direction is config.Direction.LONG else self._pe
        if contract is None:
            return False, "ATM contract not yet resolved after refresh"
        logger.info(
            "Manual %s → resolved contract %s (strike=%s expiry=%s token=%s)",
            direction.value, contract.symbol, contract.strike,
            contract.expiry, contract.token,
        )
        # Remember the engine + advisory snapshot so the post-fill row in the
        # trades table can record which engine triggered the entry.
        self._pending_engine = engine
        self._pending_confidence = confidence
        self._pending_reasons = reasons or []
        ok = self._place_entry(
            direction, contract,
            sl_pts=0.0, tp_pts=0.0,                     # unused for manual
            sl_pct=config.MANUAL_SL_PCT,
            tp_pct=config.MANUAL_TP_PCT,
            trail_step_pct=config.TRAIL_STEP_PCT,
            source="manual",
            lot_override=lots_override,
        )
        if ok:
            return (
                True,
                f"manual {direction.value} placed [{engine.upper()}] · "
                f"{contract.symbol} · "
                f"SL={config.MANUAL_SL_PCT*100:.0f}%  TP={config.MANUAL_TP_PCT*100:.0f}%  "
                f"trail={config.TRAIL_STEP_PCT*100:.0f}%",
            )
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
                lots_override = data.get("lots")
                if isinstance(lots_override, str):
                    try:
                        lots_override = int(lots_override)
                    except ValueError:
                        lots_override = None
                engine = (data.get("engine") or "indicator").lower()
                confidence = data.get("confidence")
                reasons = data.get("reasons") or []
                ok, msg = self._handle_manual_entry(
                    direction,
                    lots_override=lots_override,
                    engine=engine,
                    confidence=confidence,
                    reasons=reasons,
                )
                self.db.complete_command(cmd_id, ok, msg)
                logger.info("Manual command #%d → %s (%s)", cmd_id, "OK" if ok else "FAIL", msg)
            elif action == "panic_exit":
                if self.positions.has_open_position:
                    self._exit_reason_hint = config.ExitReason.MANUAL.value
                    self._transition(config.State.FORCED_EXIT)
                    self.db.complete_command(cmd_id, True, "FORCED_EXIT triggered")
                else:
                    self.db.complete_command(cmd_id, False, "no open position to exit")
            elif action == "reset_breakers":
                prev = self.reset_breakers()
                self.db.complete_command(
                    cmd_id, True,
                    f"reset OK (was: trades={prev['trades_today']}, "
                    f"losses={prev['consecutive_losses']}, state={prev['state']})",
                )
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
        # P0-6: entering SHUTDOWN must invalidate every queued command so a
        # stale manual_entry can never fire when we later flip back to IDLE
        # (e.g. via Reset Breakers). Done OUTSIDE the state lock to avoid
        # any chance of contention with the DB thread lock.
        if new_state is config.State.SHUTDOWN:
            try:
                n = self.db.cancel_pending_commands(reason="shutdown_cancelled")
                if n:
                    logger.warning(
                        "SHUTDOWN — cancelled %d pending command(s) to prevent stale fires.",
                        n,
                    )
            except Exception:
                logger.exception("Failed to cancel pending commands on SHUTDOWN.")

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
        # PART 3 — only two execution rules now:
        #   1. Hard daily-trade cap (MAX_TRADES_DAILY, default 3)
        #   2. Single-position lock (enforced by PositionManager itself)
        # The consecutive-losses breaker has been removed per user request —
        # they want trade-count discipline, not loss-streak lockouts.
        if self._trades_today >= config.MAX_TRADES_DAILY:
            return "max_trades_daily"
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
            # SMC engine consumes a dedicated 5m series for the spot — fully
            # parallel to the indicator engine, which never reads it.
            self.candles.series(t.token, 5).ingest_tick(t.ltp, t.volume)
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
            # PART 3 §11 — heartbeat a 'connected' status so the dashboard
            # can show a 🟢 broker badge independent of supervisor state.
            self.db.set_state(
                "broker_status",
                json.dumps({"state": "connected", "ts": time.time()}),
            )
        except Exception:
            pass

    def _on_order(self, ev: OrderEvent) -> None:
        # Once terminal, ignore everything (prevents SHUTDOWN ↔ FORCED_EXIT loop)
        if self.state is config.State.SHUTDOWN:
            return
        if ev.status == "heartbeat_lapse":
            logger.critical("Heartbeat lapse — routing to FORCED_EXIT")
            try:
                import json
                self.db.set_state(
                    "broker_status",
                    json.dumps({"state": "disconnected", "ts": time.time()}),
                )
            except Exception:
                pass
            self._exit_reason_hint = config.ExitReason.HEARTBEAT.value
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
        # P0-3: prefer the broker's ACTUAL average execution price over the
        # order-slot "price" field (which is 0 for MARKET, and the LIMIT
        # value — not the fill — for LIMIT). For SIM this is a no-op because
        # force_order() sets both fields to the same synthesised value.
        fill_px = float(ev.avg_price) if ev.avg_price and ev.avg_price > 0 else float(ev.fill_price or 0.0)
        pending = self.positions.pending_entry
        if pending and ev.order_id == pending.order_id:
            # Slippage check — wider tolerance for MARKET orders since some
            # slippage is the explicit trade-off for guaranteed fills.
            tol = (
                config.MARKET_FILL_TOLERANCE_PCT
                if config.ENTRY_ORDER_TYPE == "MARKET"
                else config.FILL_TOLERANCE_PCT
            )
            if fill_px > pending.expected_price * (1 + tol):
                logger.warning(
                    "Fill %.2f exceeds %.0f%% tolerance vs ref %.2f — emergency exit.",
                    fill_px, tol * 100, pending.expected_price,
                )
                self.positions.clear_pending()
                self._transition(config.State.FORCED_EXIT)
                return
            if fill_px <= 0:
                # Defensive guard — should never happen after the avg_price
                # preference, but if the broker ever returns neither field
                # populated we must NOT anchor SL/TP to zero.
                logger.error(
                    "Fill event has no usable price (fill=%.2f avg=%.2f) — aborting.",
                    ev.fill_price or 0.0, ev.avg_price or 0.0,
                )
                self.positions.clear_pending()
                self._transition(config.State.FORCED_EXIT)
                return
            pos = self.positions.promote_to_open(fill_px)
            source = self._pending_source if self._pending_source else "auto"
            engine = self._pending_engine if self._pending_engine else None
            confidence = self._pending_confidence
            reasons = self._pending_reasons or []
            self._pending_source = None
            self._pending_engine = None
            self._pending_confidence = None
            self._pending_reasons = []
            self.db.insert_trade_entry(
                pos.trade_id, pos.direction.value, pos.qty, pos.entry_price,
                source=source,
                engine=engine,
                confidence=confidence,
                reasons=reasons,
                sl_price=pos.stop_price,
                tp_price=pos.target_price,
                # P0-4: persist the exact contract that was executed
                contract_symbol=pos.contract_symbol,
                contract_token=pos.contract_token,
                strike=pos.strike or None,
                expiry=pos.expiry or None,
                option_type=pos.option_type or None,
                lot_size=pos.lot_size or None,
            )
            self._place_protective_legs(pos.target_price, pos.stop_price)
            self._transition(config.State.POSITION_OPEN)
            return

        # Exit-leg fill — same avg_price preference for correct PnL.
        pos = self.positions.open_position
        if pos and ev.order_id in (pos.target_order_id, pos.stop_order_id):
            self._finalize_exit(fill_px, was_stop=ev.order_id == pos.stop_order_id)

    # ────────────────────────────────────────────────────────── order flow
    def _place_entry(
        self, direction: config.Direction, contract: OptionContract, sl_pts: float, tp_pts: float,
        source: str = "auto",
        sl_pct: float = 0.0, tp_pct: float = 0.0, trail_step_pct: float = 0.0,
        lot_override: Optional[int] = None,
    ) -> bool:
        # P0-2: prefer the live WebSocket quote; fall back to a one-shot REST
        # LTP ONLY when the WS has no tick yet for this token (typical when
        # the strike was just re-picked and never subscribed). SL/TP anchor
        # to that real price; subsequent trailing/exits continue via WS.
        quote = self._last_option_quote.get(contract.token)
        premium = float((quote or {}).get("ltp") or 0.0)
        if premium <= 0:
            try:
                premium = float(self.broker.ltp(
                    contract.exchange, contract.symbol, contract.token,
                ) or 0.0)
                logger.info(
                    "REST LTP fallback for %s (token=%s) → ₹%.2f",
                    contract.symbol, contract.token, premium,
                )
            except Exception as exc:
                logger.warning(
                    "REST LTP fallback failed for %s: %s", contract.symbol, exc,
                )
                premium = 0.0
        if premium <= 0:
            if config.SIMULATE_ORDERS:
                # SIM must never fabricate a fill on an unknown price; abort
                # cleanly instead so the operator sees the failure.
                logger.warning(
                    "No premium for %s (WS empty, REST failed) — aborting SIM entry.",
                    contract.symbol,
                )
                return False
            logger.info("No quote for %s yet; skipping.", contract.symbol)
            return False

        # Premium-spike guard
        lots = self.sizer.premium_spike_guard(
            option_premium=premium,
            effective_lots=self._effective_lots,
            current_equity=self.broker.get_net_available_cash(),
        )
        # PART 3 §5 — user may override the auto-calculated lot size
        # before clicking Buy. Honour their value (still subject to the
        # spike guard so we never break capital limits).
        if lot_override is not None and lot_override > 0:
            lots = min(lots, lot_override) if lots > 0 else lot_override
            lots = max(1, lots)
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
        # PositionManager.promote_to_open(). Manual mode uses % of premium;
        # auto path stays on ATR-derived points.
        if sl_pct > 0 and tp_pct > 0:
            target_px = premium * (1 + tp_pct)
            stop_px = max(0.05, premium * (1 - sl_pct))
        else:
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
                # P0-4: full contract identity travels with the pending entry
                strike=float(contract.strike or 0.0),
                expiry=contract.expiry or "",
                option_type=contract.option_type or "",
                lot_size=int(contract.lot_size or config.LOT_SIZE_NIFTY),
                sl_points=sl_pts,
                tp_points=tp_pts,
                sl_pct=sl_pct,
                tp_pct=tp_pct,
                trail_step_pct=trail_step_pct,
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

    def _finalize_exit(
        self, exit_price: float, was_stop: bool,
        reason: Optional[str] = None,
    ) -> None:
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
        # Prefer an explicit reason if the caller supplied one — that lets
        # MAX_HOLD, SQUARE_OFF, MANUAL, and HEARTBEAT paths label themselves
        # correctly instead of everything being flattened to STOP_LOSS/TARGET.
        if reason:
            resolved = reason
        elif was_stop:
            resolved = config.ExitReason.STOP_LOSS.value
        else:
            resolved = config.ExitReason.TARGET.value
        self.db.update_trade_exit(pos.trade_id, exit_price, pnl, resolved)
        self.pnl_guard.add_realized(pnl)
        self._trades_today += 1
        self._consecutive_losses = self._consecutive_losses + 1 if pnl < 0 else 0
        logger.info(
            "Trade closed %s pnl=₹%.2f trades_today=%d consec_loss=%d",
            resolved, pnl, self._trades_today, self._consecutive_losses,
        )
        self._enter_cooldown()

    def _enter_cooldown(self) -> None:
        self._cooldown_until = datetime.now(timezone.utc) + timedelta(
            minutes=config.COOLDOWN_AFTER_EXIT_MIN
        )
        self._transition(config.State.COOLDOWN)

    # ────────────────────────────────────────────────────────── FSM steps
    def _step_idle(self) -> None:
        # PART 3 §3 — application must NEVER auto-open a trade. The
        # auto-entry pipeline below is preserved for backward compatibility
        # but skipped by default. Re-enable via AUTO_ENTRY_ENABLED=true.
        if not config.AUTO_ENTRY_ENABLED:
            self._update_signal_diag("auto-entry disabled · manual-only mode")
            return
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
            # Also update the weighted setup score (Task 1)
            self._update_setup_score(snap)
        except Exception:
            pass

    def _update_setup_score(self, snap) -> None:
        """Weighted Setup Score (Task 1). Writes to bot_state['setup_score']
        atomically via SQLite — surfaces in /api/bot/status for the UI."""
        try:
            import json
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td

            base_call = 0
            base_put = 0
            if snap.last_close is not None and snap.ema20_15m is not None:
                if snap.last_close > snap.ema20_15m: base_call += 20
                if snap.last_close < snap.ema20_15m: base_put += 20
            if snap.ema9 is not None and snap.ema21 is not None:
                if snap.ema9 > snap.ema21: base_call += 20
                if snap.ema9 < snap.ema21: base_put += 20
            if snap.adx is not None and snap.adx > config.ADX_MIN:
                base_call += 10
                base_put += 10
            if snap.adx is not None and snap.adx_prev is not None:
                if (snap.adx - snap.adx_prev) > config.ADX_DELTA_MIN:
                    base_call += 15
                    base_put += 15
            if snap.last_close is not None and snap.vwap is not None:
                if snap.last_close > snap.vwap: base_call += 15
                if snap.last_close < snap.vwap: base_put += 15
            vix = self.vix.value
            if vix is not None and config.VIX_MIN <= vix <= config.VIX_MAX:
                base_call += 10
                base_put += 10

            # Liquidity penalty — median spread over the last 3 quote updates
            # per leg. Keeps the exact formula and 50-point cap, but immunises
            # the setup score against a single stale/spike WebSocket quote:
            # a persistent wide spread still hits 50, a one-off spike gets
            # medianed out by the next tight reading.
            from collections import deque as _deque
            penalties = []
            for c in (self._ce, self._pe):
                if c is None: continue
                q = self._last_option_quote.get(c.token)
                if not q: continue
                bid, ask = q.get("bid", 0), q.get("ask", 0)
                if bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / ((ask + bid) / 2)
                    hist = self._spread_history.setdefault(c.token, _deque(maxlen=3))
                    hist.append(spread_pct)
                    ordered = sorted(hist)
                    smoothed_spread = ordered[len(ordered) // 2]   # median
                    penalties.append(min(smoothed_spread * 2000, 50))
            penalty = sum(penalties) / len(penalties) if penalties else 0

            call_score = max(0, round(base_call - penalty))
            put_score = max(0, round(base_put - penalty))

            def classify(s):
                # Bands rebalanced to the achievable base-score range (0-70).
                # Max theoretical base = EMA20 + ADX10 + ADX-Δ15 + VWAP15 + VIX10 = 70.
                # Previous thresholds treated max as 100, making STRONG (≥80)
                # unreachable. Same 5-tier vocabulary, calibrated bands.
                if s >= 60: return "STRONG"     # ≥ 86 % of max
                if s >= 45: return "GOOD"       # ≥ 64 %
                if s >= 30: return "NEUTRAL"    # ≥ 43 %
                if s >= 15: return "WEAK"       # ≥ 21 %
                return "AVOID"

            if call_score > put_score:
                bias, strength = "CALL", classify(call_score)
            elif put_score > call_score:
                bias, strength = "PUT", classify(put_score)
            else:
                bias, strength = "NEUTRAL", classify(max(call_score, put_score))

            ist_now = (_dt.now(_tz.utc) + _td(hours=5, minutes=30)).strftime("%H:%M:%S")
            self.db.set_state("setup_score", json.dumps({
                "call_score": call_score,
                "put_score": put_score,
                "base_call": base_call,
                "base_put": base_put,
                "penalty": round(penalty, 1),
                "bias": bias,
                "strength": strength,
                "timestamp": ist_now,
            }))
        except Exception:
            pass

    # ─────────────────────────────────────────────── SMC advisory (independent)
    # Smart Money Concepts engine — completely decoupled from the indicator
    # engine above. Own 5m/15m timeframes, own 09:20–15:00 IST window, own
    # state row in bot_state. Indicator-engine logic is NOT touched.
    def _update_smc_score(self) -> None:
        try:
            import json
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td

            t_ist = _ist_time()
            in_window = config.SMC_WINDOW_START <= t_ist <= config.SMC_WINDOW_END
            bars_5m = self.candles.series(config.NIFTY_SPOT_TOKEN, 5).closed_bars()
            bars_15m = self.candles.series(config.NIFTY_SPOT_TOKEN, 15).closed_bars()

            now_utc = _dt.now(_tz.utc)
            ist_now = (now_utc + _td(hours=5, minutes=30)).strftime("%H:%M:%S")

            if not in_window:
                # outside SMC window — flush any pending signal so a stale one
                # doesn't survive the lunch break.
                if self._smc_signal is not None:
                    self._smc_signal = None
                self.db.set_state("smc_score", json.dumps({
                    "direction": "NEUTRAL",
                    "confidence": 0,
                    "grade": "OFF",
                    "strength": "OFF",
                    "reasons": ["outside SMC window 09:20–15:15 IST"],
                    "entry": None, "stop_loss": None, "target": None,
                    "market_structure": "—",
                    "htf_trend": "—",
                    "regime": "—",
                    "signal_age_sec": None,
                    "signal_max_age_sec": config.MAX_SIGNAL_AGE_MINUTES * 60,
                    "bars_5m": len(bars_5m), "bars_15m": len(bars_15m),
                    "timestamp": ist_now,
                }))
                return

            result = smc_evaluate(bars_5m, bars_15m)

            # ── Signal freshness: emit, age, expire (with logging)
            signal_age_sec: Optional[int] = None
            expired = False
            if result.direction in ("CALL", "PUT") and result.confidence > 0:
                if (
                    self._smc_signal is None
                    or self._smc_signal["direction"] != result.direction
                ):
                    # New signal — start the freshness clock and log it
                    self._smc_signal = {
                        "direction": result.direction,
                        "confidence": result.confidence,
                        "grade": _smc_grade(result.confidence),
                        "generated_at": now_utc.isoformat(),
                    }
                    logger.info(
                        "SMC BUY %s (%d%%) generated  grade=%s  HTF=%s  structure=%s",
                        result.direction, result.confidence,
                        _smc_grade(result.confidence),
                        (result.ctx.htf_trend if result.ctx else "—"),
                        (result.ctx.structure if result.ctx else "—"),
                    )
                # Age check
                gen = _dt.fromisoformat(self._smc_signal["generated_at"])
                age = (now_utc - gen).total_seconds()
                if age >= config.MAX_SIGNAL_AGE_MINUTES * 60:
                    logger.info(
                        "SMC signal expired (age %.0fs ≥ %dm) — re-scanning",
                        age, config.MAX_SIGNAL_AGE_MINUTES,
                    )
                    self._smc_signal = None
                    expired = True
                    signal_age_sec = int(age)
                else:
                    signal_age_sec = int(age)
            else:
                # NEUTRAL or zero confidence — drop any active signal
                if self._smc_signal is not None:
                    self._smc_signal = None

            ctx = result.ctx
            grade = _smc_grade(result.confidence) if not expired else "EXPIRED"
            payload = {
                "direction": "NEUTRAL" if expired else result.direction,
                "confidence": 0 if expired else result.confidence,
                "grade": grade,
                "strength": "EXPIRED" if expired else smc_classify(result.confidence),
                "reasons": (["signal expired — re-scanning"] if expired
                            else list(result.reasons or [])),
                "entry": None if expired else result.entry,
                "stop_loss": None if expired else result.stop_loss,
                "target": None if expired else result.target,
                "market_structure": (ctx.structure if ctx else "NEUTRAL"),
                "htf_trend": (ctx.htf_trend if ctx else "NEUTRAL"),
                "regime": (ctx.regime if ctx else "UNCLEAR"),
                "signal_age_sec": signal_age_sec,
                "signal_max_age_sec": config.MAX_SIGNAL_AGE_MINUTES * 60,
                "bars_5m": len(bars_5m),
                "bars_15m": len(bars_15m),
                "timestamp": ist_now,
            }
            self.db.set_state("smc_score", json.dumps(payload))
            # Advisory notification — swallows all Telegram errors internally.
            try:
                self.telegram.maybe_notify_smc(payload)
            except Exception:
                logger.exception("Telegram maybe_notify_smc raised (ignored)")
        except Exception:
            logger.debug("SMC scoring tick failed (continuing)", exc_info=True)

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
        if self._past_square_off():
            self._exit_reason_hint = config.ExitReason.SQUARE_OFF.value
            self._transition(config.State.FORCED_EXIT)
            return
        if pos.age_seconds() >= config.MAX_HOLD_TIME_MIN * 60:
            self._exit_reason_hint = config.ExitReason.TIME_STOP.value
            self._transition(config.State.FORCED_EXIT)
            return

        q = self._last_option_quote.get(pos.contract_token, {})
        raw_ltp = q.get("ltp")

        # 3-tick median smoothing on the LTP used for synthetic SL/TP and
        # trailing-SL evaluation. Same source quote as before; only the value
        # fed into the SL/TP comparators is the median of the last 3 reads.
        # Purpose: absorb a single transient wide bid-ask spike so a healthy
        # position isn't flushed on one bad tick. SL/TP thresholds, trail
        # step %, exit routing, and order placement are all unchanged — the
        # ONLY difference is that `ltp` below is the smoothed price.
        ltp = None
        if raw_ltp is not None and raw_ltp > 0:
            from collections import deque as _deque
            hist = self._ltp_history.setdefault(pos.contract_token, _deque(maxlen=3))
            hist.append(raw_ltp)
            ordered = sorted(hist)
            ltp = ordered[len(ordered) // 2]   # median of last ≤3 ticks

        # ── Synthetic SL/TP enforcement (PRIMARY in SIM, SAFETY-NET in LIVE)
        # The protective legs we placed on the broker may not fire on time:
        #   • SIM mode  — no exchange runs against them, so they NEVER fire.
        #     This block is the only enforcement path.
        #   • LIVE mode — if a gap or network hiccup delays the broker fill,
        #     the bot self-rescues by firing a MARKET sell here. A subsequent
        #     broker SL fill simply finds nothing to flatten.
        if ltp is not None and ltp > 0:
            if ltp >= pos.target_price:
                logger.info(
                    "LTP ₹%.2f ≥ target ₹%.2f → firing TARGET exit",
                    ltp, pos.target_price,
                )
                self._synthetic_exit(ltp, was_stop=False)
                return
            if ltp <= pos.stop_price:
                logger.info(
                    "LTP ₹%.2f ≤ stop ₹%.2f → firing STOP_LOSS exit",
                    ltp, pos.stop_price,
                )
                self._synthetic_exit(ltp, was_stop=True)
                return

        # trail stop using latest premium (same smoothed LTP)
        if ltp:
            new_stop = self.positions.maybe_trail_stop(ltp)
            if new_stop is not None and pos.stop_order_id:
                # PART 3 §15 — cancel-replace SL atomically. If the cancel
                # succeeds but the replace fails, the position has NO active
                # stop on the broker → force-exit immediately rather than
                # leaving it unprotected.
                try:
                    self.broker.cancel_order(pos.stop_order_id)
                except Exception:
                    logger.exception(
                        "Trail-cancel failed — old SL still active at ₹%.2f, skipping bump",
                        pos.stop_price,
                    )
                    return
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
                try:
                    new_id = self.broker.place_order(new_sl_payload)
                    self.positions.set_protective_orders(pos.target_order_id, new_id)
                    logger.info(
                        "Stop trailed → ₹%.2f (%s order=%s)",
                        new_stop, config.SL_ORDER_TYPE, new_id,
                    )
                except Exception:
                    logger.critical(
                        "Trail SL re-place FAILED at ₹%.2f — position unprotected, "
                        "routing to FORCED_EXIT to flatten.", new_stop,
                    )
                    self._transition(config.State.FORCED_EXIT)

    def _synthetic_exit(self, exit_price: float, was_stop: bool) -> None:
        """Bot-driven exit (PART 3 §15 safety net + SIM-mode enforcement).
        Cancels both protective legs, fires a MARKET sell to flatten, and
        finalises the trade. Idempotent — if anything is already gone the
        broker errors are swallowed."""
        pos = self.positions.open_position
        if pos is None:
            return
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
            logger.exception("Synthetic exit market sell failed (continuing)")
        self._finalize_exit(exit_price, was_stop=was_stop)

    def reset_breakers(self) -> dict:
        """Admin reset for circuit-breaker counters. Lets the user clear an
        accumulated SIM-mode lockout (or recover from a LIVE-mode breach
        once they've reviewed the cause) without restarting the daemon.

        P0-6: cancels every pending/running command BEFORE flipping the FSM
        back to IDLE, so a queued manual_entry the user submitted while in
        SHUTDOWN cannot suddenly execute the moment the state clears.
        """
        prev = {
            "trades_today": self._trades_today,
            "consecutive_losses": self._consecutive_losses,
            "api_reject_count": self._api_reject_count,
            "state": self.state.value,
        }
        try:
            cancelled = self.db.cancel_pending_commands(reason="reset_breakers_cancelled")
        except Exception:
            logger.exception("cancel_pending_commands failed during reset_breakers")
            cancelled = 0
        self._trades_today = 0
        self._consecutive_losses = 0
        self._api_reject_count = 0
        if self.state is config.State.SHUTDOWN and not self.positions.has_open_position:
            self._transition(config.State.IDLE)
        logger.warning(
            "Circuit-breaker counters reset by user. Previous: %s. "
            "Cancelled %d pending command(s).",
            prev, cancelled,
        )
        prev["cancelled_commands"] = cancelled
        return prev

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
        # Consume the exit-reason hint set by whoever routed us here. If no
        # hint (e.g. a bug path we forgot to tag), fall back to STOP_LOSS to
        # preserve the old behaviour instead of silently misreporting.
        reason = self._exit_reason_hint or config.ExitReason.STOP_LOSS.value
        self._exit_reason_hint = None
        self._finalize_exit(ltp, was_stop=(reason != config.ExitReason.TARGET.value), reason=reason)

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

                # Continuously refresh the setup-score advisory (Task 1)
                # so the UI shows live scoring across all FSM states.
                self._update_signal_diag("tick")

                # SMC Engine — completely independent of the indicator engine.
                # Own 5m/15m series, own 09:20–15:00 IST window, own state key.
                self._update_smc_score()

                # P0-5: keep the atm_snapshot fresh (~every 10s) so the
                # dashboard confirmation modal always shows the actual
                # strike, expiry and premium that will be traded. This is
                # display-only — the truth of what gets executed still
                # comes from _refresh_atm_contracts() at click time.
                now_ts = time.time()
                if now_ts - self._last_atm_publish_ts >= 10.0:
                    try:
                        self._refresh_atm_contracts()
                    except Exception:
                        logger.exception("Periodic ATM snapshot refresh failed.")
                    self._last_atm_publish_ts = now_ts

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
                self._exit_reason_hint = config.ExitReason.HEARTBEAT.value
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
