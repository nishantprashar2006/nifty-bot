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
# v2.2 — Fixed capital→lots sizing.
# Single source of truth for every code path (manual + auto + sim + live).
# ────────────────────────────────────────────────────────────────────
FIXED_RISK_PCT       = 2.5   # locked, not user-editable
FIXED_SL_PCT_DISPLAY = 15.0
FIXED_TP_PCT_DISPLAY = 30.0
FIXED_TRAIL_PCT_DISPLAY = 10.0


def calculate_execution_lots(capital: float) -> int:
    """v2.2 — deterministic capital→lots mapping.

    Single implementation shared by manual entries, auto entries, SIM
    and LIVE. If capital cannot be determined (e.g. LIVE RMS lookup
    fails), the caller MUST refuse the trade — never fall back.

        <  ₹50,000  → 2 lots
       50-80k       → 3 lots
       80-150k      → 4 lots
      150-200k      → 5 lots
       ≥ ₹200,000  → 6 lots
    """
    c = float(capital or 0.0)
    if c < 0:
        return 2
    if c < 50_000:
        return 2
    if c < 80_000:
        return 3
    if c < 150_000:
        return 4
    if c < 200_000:
        return 5
    return 6


# ────────────────────────────────────────────────────────────────────
# v1.14 — Broker-safe tick rounding.
#
# NFO options exchange tick size is ₹0.05 (5 paise). Angel One rejects
# any order (or trigger) price not on a 5-paise grid with:
#   "Please set your order price in multiples of 5 paise and place order again."
# All broker-bound prices in this module MUST go through this helper.
# Round-half-away-from-zero on the tick grid, snapped to 2 decimals
# to eliminate float noise.
# ────────────────────────────────────────────────────────────────────
def _tick_round(price: float, tick: float = 0.05) -> float:
    if price is None:
        return price
    p = float(price)
    if tick <= 0:
        return round(p, 2)
    n = round(p / tick)
    return round(n * tick, 2)


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

        # v1.10 — isolated observability writer. Every meaningful execution
        # event is appended here; the UI reads it via /api/bot/trade/{id}/timeline.
        # Never influences trading decisions.
        from execution_timeline import TimelineLogger
        self.timeline = TimelineLogger(config.DB_PATH)
        # Session key for the current pending entry (rewritten to real
        # trade_id on fill via `timeline.rekey_session`).
        self._timeline_session: Optional[str] = None

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

        # v1.13 — structured rejection context for the last failed entry
        # attempt (pre-flight or broker). Cleared on every new entry click;
        # consumed by _handle_manual_entry so the UI can surface the real
        # reason (available/required funds, broker message, error code).
        self._last_reject_context: Optional[dict] = None

        # v1.15 — auto-trade safety flag. Set to a non-empty string when a
        # protection failure / broker outage occurs. While set, the auto
        # entry gate refuses to fire even if AUTO mode is on. Cleared only
        # by an explicit dashboard "resume" action.
        self._auto_suspended_reason: Optional[str] = None

        # v2.0.1 — de-dup key for a failed auto signal so `_maybe_auto_entry`
        # doesn't retry the same (direction, confidence-bucket, ts) every
        # 5m SMC tick. Cleared on any meaningful state change (new signal,
        # direction flip, user resume, mode toggle).
        self._last_auto_failure_key: Optional[tuple] = None

    def _tl(self, trade_id: str, event_type: str, message: str, payload: Optional[dict] = None) -> None:
        """v1.10 — SAFE timeline logging shim.

        Wraps `self._tl(...)` with a getattr guard so any code
        path that reaches this method without an initialised timeline
        (e.g. tests using `NiftyOptionsBot.__new__`) is a silent no-op
        rather than an AttributeError. Also swallows any exception
        raised inside the writer — the trading loop never crashes
        because of an observability log call.
        """
        tl = getattr(self, "timeline", None)
        if tl is None or not trade_id:
            return
        try:
            tl.log(trade_id, event_type, message, payload)
        except Exception:
            pass

    def _tl_rekey(self, session_id: Optional[str], trade_id: str) -> None:
        """Same guarded shim for rekey_session."""
        tl = getattr(self, "timeline", None)
        if tl is None or not session_id or not trade_id:
            return
        try:
            tl.rekey_session(session_id, trade_id)
        except Exception:
            pass

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
        # v1.14 — Phase Y: transparent audit wrapper. Records every
        # placeOrder / modifyOrder / cancelOrder / order_book / positions
        # call into an in-memory ring AND a SQLite table for cross-process
        # (server) visibility. Trading behaviour unchanged — observer only.
        from broker_audit import BrokerAudit
        self.broker_audit = BrokerAudit(
            capacity=100,
            sink=lambda e: self.db.record_broker_audit(e),
        )
        self.broker = self.broker_audit.wrap(self.broker)

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
            tg = getattr(self, "telegram", None)
            if tg is not None:
                tg.send_startup()
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

            # v1.9 P1 — if we snapshotted the live protection state before
            # restart, restore trail_anchor / bumps / hi-lo from it. Guard
            # by trade_id + token so a stale snapshot from a previous trade
            # never contaminates the recovery.
            trail_anchor = entry_px
            trail_bumps = 0
            initial_stop = stop_px
            initial_target = target_px
            hi_ltp = entry_px
            lo_ltp = entry_px
            trail_step_pct = config.TRAIL_STEP_PCT
            try:
                import json as _json
                snap_row = self.db.get_state("live_position")
                if snap_row:
                    snap = _json.loads(snap_row[0])
                    if (snap.get("trade_id") == trade_id
                            and str(snap.get("contract_token")) == token):
                        stop_px = float(snap.get("stop_price") or stop_px)
                        trail_anchor = float(snap.get("trail_anchor") or entry_px)
                        trail_bumps = int(snap.get("trail_bumps") or 0)
                        initial_stop = float(snap.get("initial_stop_price") or stop_px)
                        initial_target = float(snap.get("initial_target_price") or target_px)
                        hi_ltp = float(snap.get("highest_ltp_seen") or entry_px)
                        lo_ltp = float(snap.get("lowest_ltp_seen") or entry_px)
                        trail_step_pct = float(snap.get("trail_step_pct") or config.TRAIL_STEP_PCT)
                        logger.warning(
                            "Boot recovery: restored trail state — anchor=₹%.2f "
                            "stop=₹%.2f bumps=%d hi=₹%.2f",
                            trail_anchor, stop_px, trail_bumps, hi_ltp,
                        )
            except Exception:
                logger.exception("live_position snapshot restore failed (using defaults)")

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
                trail_anchor=trail_anchor,
                trail_step_pct=trail_step_pct,
                initial_stop_price=initial_stop,
                initial_target_price=initial_target,
                trail_bumps=trail_bumps,
                highest_ltp_seen=hi_ltp,
                lowest_ltp_seen=lo_ltp,
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

        P0-Q1: after resolving new strikes, this method also pushes a fresh
        WebSocket subscription list to the LIVE ws (if any) so ticks start
        flowing for the newly-picked tokens immediately — no reconnect,
        no stale-LTP window on the dashboard.

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
            old_tokens = {
                self._ce.token if self._ce else None,
                self._pe.token if self._pe else None,
            }
            self._ce = ce
            self._pe = pe
            new_tokens = {ce.token, pe.token}
            logger.info(
                "ATM picks → CE=%s (token=%s), PE=%s (token=%s) [spot=%.2f]",
                ce.symbol, ce.token, pe.symbol, pe.token, spot,
            )
            # P0-Q1: if the token set changed, push the new subs to the WS
            # so LTPs for the freshly-picked strikes actually reach us.
            if self.ws is not None and new_tokens != old_tokens:
                try:
                    subs = self._build_token_subscriptions()
                    accepted = self.ws.resubscribe(subs)
                    if accepted:
                        logger.info(
                            "WS resubscribed after ATM shift (new tokens: %s)",
                            sorted(t for t in new_tokens if t),
                        )
                    else:
                        logger.warning(
                            "WS resubscribe not applied live (socket down?); "
                            "subs cached for next reconnect.",
                        )
                except Exception:
                    logger.exception("WS resubscribe raised.")
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
        # v1.13 — clear any stale rejection context from a previous click
        # so the UI never shows a rejection banner for an unrelated attempt.
        self._last_reject_context = None
        # v1.10 — start a new timeline session for this click. All events
        # before we know the real trade_id are recorded under this session
        # key, then rewritten to the real trade_id inside _handle_fill.
        from execution_timeline import new_session_id, Event
        self._timeline_session = new_session_id()
        self._tl(
            self._timeline_session, Event.ENTRY_CLICK,
            f"Manual BUY {direction.value} clicked",
            {"direction": direction.value, "engine": engine,
             "lots_override": lots_override, "confidence": confidence},
        )
        # P0-1: mandatory pre-entry refresh. Never trust startup cache.
        try:
            self._refresh_atm_contracts()
            self._tl(
                self._timeline_session, Event.ATM_REFRESH,
                "ATM contracts refreshed",
            )
        except Exception:
            logger.exception("Pre-entry ATM refresh failed; aborting manual entry.")
            self._tl(
                self._timeline_session, Event.NOTE,
                "ATM refresh failed — entry aborted",
            )
            return False, "ATM refresh failed — could not resolve current strike"
        contract = self._ce if direction is config.Direction.LONG else self._pe
        if contract is None:
            return False, "ATM contract not yet resolved after refresh"
        self._tl(
            self._timeline_session, Event.CONTRACT_SELECTED,
            f"Selected contract {contract.symbol}",
            {"symbol": contract.symbol, "token": contract.token,
             "strike": contract.strike, "expiry": contract.expiry,
             "option_type": contract.option_type, "lot_size": contract.lot_size},
        )
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
        # v1.13 — surface the structured rejection reason if one was captured
        # by `_place_entry` (pre-flight failure OR broker SmartApiError).
        ctx = self._last_reject_context
        if ctx:
            import json as _json
            tag = "PRECHECK_FAILED" if ctx.get("phase") == "preflight" else "BROKER_REJECTED"
            return False, f"{tag}: {_json.dumps(ctx, separators=(',', ':'))}"
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
            elif action == "refresh_atm":
                # P0-Q2: on-demand ATM refresh. Fired by the dashboard when
                # the confirmation modal opens (or the operator hits a
                # 'refresh contract' button). Keeps REST usage tied to
                # user intent rather than a background timer.
                try:
                    self._refresh_atm_contracts()
                    self.db.complete_command(cmd_id, True, "ATM refreshed")
                except Exception as exc:
                    self.db.complete_command(cmd_id, False, f"refresh_atm failed: {exc}")
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
            "ltp": t.ltp, "bid": t.bid, "ask": t.ask,
            "volume": t.volume, "oi": t.oi,
            "ts": t.ts, "source": "ws",
        }
        self.candles.series(t.token, 3).ingest_tick(t.ltp, t.volume)
        # If this is the open position's contract, push live LTP into
        # bot_state. Also stamp the token + freshness so the dashboard can
        # detect cross-contract bleed or a frozen stream.
        pos = self.positions.open_position
        if pos and pos.contract_token == t.token:
            self._update_live_state(option_ltp=t.ltp, option_token=t.token)

    def _update_live_state(self, spot: Optional[float] = None,
                            option_ltp: Optional[float] = None,
                            option_token: Optional[str] = None) -> None:
        try:
            import json
            current = self.db.get_state("live_quotes")
            payload = json.loads(current[0]) if current else {}
            now_ts = time.time()
            if spot is not None:
                payload["spot"] = spot
                payload["spot_ts"] = now_ts
            if option_ltp is not None:
                payload["option_ltp"] = option_ltp
                payload["option_ltp_ts"] = now_ts
                if option_token is not None:
                    payload["option_ltp_token"] = option_token
            vix_val = self.vix.value
            if vix_val is not None:
                payload["vix"] = vix_val
            payload["ts"] = now_ts
            self.db.set_state("live_quotes", json.dumps(payload))
            # PART 3 §11 — heartbeat a 'connected' status so the dashboard
            # can show a 🟢 broker badge independent of supervisor state.
            self.db.set_state(
                "broker_status",
                json.dumps({"state": "connected", "ts": now_ts}),
            )
        except Exception:
            pass

    def _publish_ws_health(self) -> None:
        """P0 diagnostics — pushes ws.health() into bot_state.ws_health for
        the dashboard. Cheap read-only snapshot; called once per tick."""
        try:
            import json as _json
            if self.ws is None:
                return
            h = self.ws.health()
            self.db.set_state("ws_health", _json.dumps(h))
        except Exception:
            # Diagnostics must never crash the main loop.
            pass

    def _persist_live_position_state(self) -> None:
        """v1.9 P1: snapshot the open position's mutable protection state
        into bot_state.live_position so an unexpected restart can resume
        trailing from the CURRENT anchor rather than resetting to entry.

        Written on every trail bump (rare) — not every tick — to keep DB
        churn low.
        """
        try:
            import json as _json
            pos = self.positions.open_position
            if pos is None:
                return
            snap = {
                "trade_id": pos.trade_id,
                "contract_token": pos.contract_token,
                "stop_price": pos.stop_price,
                "trail_anchor": pos.trail_anchor,
                "trail_bumps": pos.trail_bumps,
                "trail_step_pct": pos.trail_step_pct,
                "initial_stop_price": pos.initial_stop_price,
                "initial_target_price": pos.initial_target_price,
                "highest_ltp_seen": pos.highest_ltp_seen,
                "lowest_ltp_seen": pos.lowest_ltp_seen,
                "ts": time.time(),
            }
            self.db.set_state("live_position", _json.dumps(snap))
        except Exception:
            logger.exception("Failed to persist live_position state (ignored)")

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
            # v1.10 timeline — bind pre-fill events to the real trade_id,
            # then emit the ENTRY_FILL event under that trade_id so
            # subsequent protection/trail/exit events sit in one contiguous
            # timeline the UI can render.
            from execution_timeline import Event
            if getattr(self, "_timeline_session", None):
                self._tl_rekey(self._timeline_session, pos.trade_id)
            self._tl(
                pos.trade_id, Event.ENTRY_FILL,
                f"Entry filled ₹{fill_px:.2f}",
                {"fill_price": fill_px, "avg_price": ev.avg_price,
                 "raw_fill_price": ev.fill_price, "order_id": ev.order_id,
                 "contract_symbol": pos.contract_symbol,
                 "contract_token": pos.contract_token},
            )
            self._timeline_session = None
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
                # v1.10 timeline
                if getattr(self, "_timeline_session", None) and premium > 0:
                    from execution_timeline import Event
                    self._tl(
                        self._timeline_session, Event.REST_LTP,
                        f"REST premium fetched ₹{premium:.2f}",
                        {"symbol": contract.symbol, "token": contract.token,
                         "ltp": premium},
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
                "price": _tick_round(limit_px),
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
        # v1.13 — pre-flight margin check (advisory).
        # v2.0.1 — SIM MUST NEVER call live broker funds. Only run the
        # pre-flight when TRADING_MODE is live (execution against the real
        # exchange). SIM/paper uses fabricated capital via the sizer.
        if config.SIMULATE_ORDERS:
            required_capital = qty * limit_px * (1.0 + config.PREFLIGHT_MARGIN_BUFFER_PCT)
            available_cash = None
        else:
            # Estimates required capital ≈ qty × premium × (1 + buffer) and
            # compares against `broker.get_net_available_cash()`. If the RMS
            # call fails for any reason, we DO NOT block trading — the broker
            # remains the authoritative gate. Only obvious insufficient-funds
            # cases short-circuit here so the user never sees a phantom
            # "Queued" for an order that will be rejected in a few hundred ms.
            required_capital = qty * limit_px * (1.0 + config.PREFLIGHT_MARGIN_BUFFER_PCT)
            try:
                available_cash = float(self.broker.get_net_available_cash())
            except Exception:
                logger.warning("preflight: RMS lookup failed — deferring to broker.", exc_info=True)
                available_cash = None
        if available_cash is not None and required_capital > available_cash:
            reason = (
                f"Insufficient funds — Available ₹{available_cash:,.2f}, "
                f"Required ≈ ₹{required_capital:,.2f}"
            )
            logger.warning("PRECHECK_FAILED: %s (%s qty=%d @ ₹%.2f)",
                           reason, contract.symbol, qty, limit_px)
            self._last_reject_context = {
                "phase": "preflight",
                "broker_status": "not_submitted",
                "broker_reason": "insufficient_funds",
                "user_message": reason,
                "available": round(available_cash, 2),
                "required": round(required_capital, 2),
                "symbol": contract.symbol,
                "qty": qty,
            }
            if getattr(self, "_timeline_session", None):
                from execution_timeline import Event
                self._tl(
                    self._timeline_session, Event.PRECHECK_FAILED,
                    reason,
                    {"available": round(available_cash, 2),
                     "required": round(required_capital, 2),
                     "symbol": contract.symbol,
                     "qty": qty,
                     "ref_price": round(limit_px, 2)},
                )
            return False

        try:
            order_id = self.broker.place_order(payload)
        except SmartApiError as exc:
            self._api_reject_count += 1
            # v1.13 — capture the FULL broker rejection reason so the UI
            # can display it verbatim ("RMS: Margin Exceeds", exchange
            # rejection text, "Insufficient funds", etc.) instead of a
            # generic "broker rejected the entry".
            broker_message = str(exc) if str(exc) else "broker rejected order (no message)"
            logger.warning("Broker rejected entry: %s", broker_message)
            self._last_reject_context = {
                "phase": "broker",
                "broker_status": "rejected",
                "broker_reason": broker_message,
                "user_message": f"Broker rejected the order — {broker_message}",
                "symbol": contract.symbol,
                "qty": qty,
                "ref_price": round(limit_px, 2),
            }
            if getattr(self, "_timeline_session", None):
                from execution_timeline import Event
                self._tl(
                    self._timeline_session, Event.ORDER_REJECTED,
                    f"Broker rejected: {broker_message}",
                    {"broker": "AngelOne",
                     "reason": broker_message,
                     "symbol": contract.symbol,
                     "qty": qty,
                     "ref_price": round(limit_px, 2)},
                )
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
        # v1.10 timeline — broker order submitted (and immediately ack'd since
        # place_order returned a non-empty id). Broker fill event comes later
        # from _handle_fill.
        if getattr(self, "_timeline_session", None):
            from execution_timeline import Event
            self._tl(
                self._timeline_session, Event.ORDER_SUBMIT,
                f"Entry order submitted qty={qty} ref=₹{limit_px:.2f}",
                {"order_id": order_id, "qty": qty, "limit_px": limit_px,
                 "ordertype": config.ENTRY_ORDER_TYPE},
            )
            self._tl(
                self._timeline_session, Event.ORDER_ACK,
                f"Broker acknowledged order {order_id}",
                {"order_id": order_id},
            )

        # P0-Q1: seed `_last_option_quote[contract.token]` with the premium
        # we just placed against so the very first frame the dashboard
        # renders after this entry uses THIS contract's real price — not a
        # stale option_ltp from an earlier scenario. If a WS tick arrives
        # a moment later, it will simply overwrite this seed.
        self._last_option_quote[contract.token] = {
            "ltp": float(premium),
            "bid": float(premium),
            "ask": float(premium),
            "volume": 0,
            "oi": 0,
            "ts": time.time(),
            "source": "seed",     # cleared to 'ws' on next real tick
        }
        self._update_live_state(option_ltp=float(premium), option_token=contract.token)

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
        # v1.14 — SL/TP prices MUST be snapped to the exchange tick (5 paise).
        # Angel One rejects any protection order not on the tick grid.
        target_price = _tick_round(target_price)
        stop_price = _tick_round(stop_price)
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
            "price": target_price,
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
                "triggerprice": stop_price,
                "quantity": pos.qty,
            }
        else:
            sl = {
                **tgt,
                "ordertype": "STOPLOSS_LIMIT",
                "price": stop_price,
                "triggerprice": _tick_round(stop_price * 1.001),
            }
        # v1.14 — place each leg in its own try/except so a rejection on one
        # doesn't hide the other. Emit dedicated timeline events carrying the
        # FULL broker message so the UI can display exactly what Angel said.
        tgt_id: Optional[str] = None
        sl_id: Optional[str] = None
        tgt_err: Optional[str] = None
        sl_err: Optional[str] = None
        try:
            tgt_id = self.broker.place_order(tgt)
        except SmartApiError as exc:
            tgt_err = str(exc) or "broker rejected TP"
            logger.warning("TP_REJECTED: %s (price=₹%.2f)", tgt_err, target_price)
        try:
            sl_id = self.broker.place_order(sl)
        except SmartApiError as exc:
            sl_err = str(exc) or "broker rejected SL"
            logger.warning("SL_REJECTED: %s (trigger=₹%.2f)", sl_err, stop_price)

        # Record whichever ids we got so the FSM can still track/cancel.
        if tgt_id or sl_id:
            self.positions.set_protective_orders(tgt_id, sl_id)

        from execution_timeline import Event
        pos_for_log = self.positions.open_position
        if pos_for_log:
            if tgt_id:
                self._tl(
                    pos_for_log.trade_id, Event.TP_PLACED,
                    f"Initial Target placed ₹{target_price:.2f}",
                    {"order_id": tgt_id, "price": target_price},
                )
            else:
                self._tl(
                    pos_for_log.trade_id, Event.TP_REJECTED,
                    f"Broker rejected TP: {tgt_err}",
                    {"broker": "AngelOne", "reason": tgt_err,
                     "price": target_price, "leg": "TP"},
                )
            if sl_id:
                self._tl(
                    pos_for_log.trade_id, Event.SL_PLACED,
                    f"Initial Stop placed ₹{stop_price:.2f}",
                    {"order_id": sl_id, "price": stop_price,
                     "order_type": config.SL_ORDER_TYPE},
                )
            else:
                self._tl(
                    pos_for_log.trade_id, Event.SL_REJECTED,
                    f"Broker rejected SL: {sl_err}",
                    {"broker": "AngelOne", "reason": sl_err,
                     "price": stop_price, "leg": "SL",
                     "order_type": config.SL_ORDER_TYPE},
                )

        if not (tgt_id and sl_id):
            # At least one leg is missing at the broker. Log a failure health
            # event and route to FORCED_EXIT so the position never lives
            # unprotected — same safety envelope as before.
            if pos_for_log:
                self._tl(
                    pos_for_log.trade_id, Event.PROTECTION_HEALTH_FAIL,
                    "Protection incomplete after initial place — flattening",
                    {"tp_id": tgt_id, "sl_id": sl_id,
                     "tp_reason": tgt_err, "sl_reason": sl_err},
                )
            logger.critical(
                "Protection incomplete (tp=%s sl=%s) — flattening position.",
                tgt_id, sl_id,
            )
            # v1.15 — protection failure ⇒ suspend AUTO trading until operator resumes.
            self._suspend_auto(f"Protection incomplete: TP={tgt_err} · SL={sl_err}")
            self._exit_reason_hint = config.ExitReason.REJECTED.value
            self._transition(config.State.FORCED_EXIT)
            return

        logger.info(
            "Protective OCO armed [SL=%s]: tgt=%s sl=%s  (target ₹%.2f, stop ₹%.2f)",
            config.SL_ORDER_TYPE, tgt_id, sl_id, target_price, stop_price,
        )
        # v1.9 P1 — Post-place broker verification.
        if not self._verify_protection_legs_placed(tgt_id, sl_id):
            logger.warning(
                "Protection legs not visible in order book after place; "
                "retrying once before forcing exit.",
            )
            self._retry_missing_protection_legs(pos, target_price, stop_price)
            if not self._verify_protection_legs_placed(
                self.positions.open_position.target_order_id if self.positions.open_position else tgt_id,
                self.positions.open_position.stop_order_id if self.positions.open_position else sl_id,
            ):
                if pos_for_log:
                    self._tl(
                        pos_for_log.trade_id, Event.PROTECTION_HEALTH_FAIL,
                        "Protection legs missing in order book after retry — flattening",
                        {"tp_id": tgt_id, "sl_id": sl_id},
                    )
                logger.critical(
                    "Protection legs still missing after retry — "
                    "flattening position defensively.",
                )
                self._exit_reason_hint = config.ExitReason.REJECTED.value
                self._transition(config.State.FORCED_EXIT)
                return
        # All legs armed AND visible in the order book → health OK.
        if pos_for_log:
            self._tl(
                pos_for_log.trade_id, Event.PROTECTION_HEALTH_OK,
                "Protection health verified — both legs live at broker",
                {"tp_id": tgt_id, "sl_id": sl_id,
                 "target_price": target_price, "stop_price": stop_price},
            )

    def _verify_protection_legs_placed(
        self, tgt_id: Optional[str], sl_id: Optional[str],
    ) -> bool:
        """v1.9 P1: peek at the broker order book and confirm both protective
        legs are actually resting there. Returns True only when BOTH ids are
        found with a non-terminal status.
        """
        try:
            ob = self.broker.order_book() or []
        except Exception:
            # If we can't read the book, don't force a false-negative flatten.
            # We assume the place calls that returned an id are honoured.
            logger.exception("order_book() failed during protection verify")
            return True
        ok_ids = {tgt_id, sl_id} - {None}
        found: set[str] = set()
        for o in ob:
            oid = str(o.get("orderid") or "")
            if oid in ok_ids:
                status = str(o.get("status") or o.get("orderstatus") or "").lower()
                if status in {"open", "trigger pending", "trigger_pending", "pending"}:
                    found.add(oid)
        missing = ok_ids - found
        if missing:
            logger.warning("Protection legs missing in order book: %s", missing)
            return False
        return True

    def _retry_missing_protection_legs(
        self, pos, target_price: float, stop_price: float,
    ) -> None:
        """v1.9 P1: single-shot retry of whichever leg is missing.
        Deliberately narrow — we do NOT re-place a leg whose id we can still
        see in the order book, to avoid duplicate protection.
        """
        try:
            ob = self.broker.order_book() or []
        except Exception:
            return
        alive: set[str] = set()
        for o in ob:
            status = str(o.get("status") or o.get("orderstatus") or "").lower()
            if status in {"open", "trigger pending", "trigger_pending", "pending"}:
                alive.add(str(o.get("orderid") or ""))
        # Re-place only the missing ones
        if pos.target_order_id not in alive:
            try:
                new_tgt = self.broker.place_order({
                    "variety": "NORMAL", "tradingsymbol": pos.contract_symbol,
                    "symboltoken": pos.contract_token, "transactiontype": "SELL",
                    "exchange": "NFO", "ordertype": "LIMIT",
                    "producttype": "INTRADAY", "duration": "DAY",
                    "price": _tick_round(target_price), "quantity": pos.qty,
                })
                self.positions.set_protective_orders(new_tgt, pos.stop_order_id)
            except Exception:
                logger.exception("Retry: target leg re-place failed")
        if pos.stop_order_id not in alive:
            try:
                sl_payload = {
                    "variety": "NORMAL", "tradingsymbol": pos.contract_symbol,
                    "symboltoken": pos.contract_token, "transactiontype": "SELL",
                    "exchange": "NFO", "ordertype": config.SL_ORDER_TYPE,
                    "producttype": "INTRADAY", "duration": "DAY",
                    "price": "0" if config.SL_ORDER_TYPE == "STOPLOSS_MARKET"
                             else _tick_round(stop_price),
                    "triggerprice": _tick_round(stop_price)
                                    if config.SL_ORDER_TYPE == "STOPLOSS_MARKET"
                                    else _tick_round(stop_price * 1.001),
                    "quantity": pos.qty,
                }
                new_sl = self.broker.place_order(sl_payload)
                self.positions.set_protective_orders(pos.target_order_id, new_sl)
            except Exception:
                logger.exception("Retry: SL leg re-place failed")

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
        # v1.10 timeline — record trigger + fill BEFORE we clear the pos.
        from execution_timeline import Event
        self._tl(
            pos.trade_id, Event.EXIT_FILL,
            f"Exit filled ₹{exit_price:.2f}  PnL ₹{pnl:+.2f}",
            {"exit_price": exit_price, "pnl": pnl, "was_stop": was_stop,
             "explicit_reason": reason},
        )
        # v1.9 — distinguish INITIAL_SL from TRAILING_STOP. If the current
        # stop is above the initial stop (any bump ever happened) and the
        # exit was on the stop side, it's a trailing stop, not a raw SL.
        if reason:
            resolved = reason
        elif was_stop:
            if pos.trail_bumps > 0 and pos.stop_price > pos.initial_stop_price:
                resolved = config.ExitReason.TRAILING_STOP.value
            else:
                resolved = config.ExitReason.STOP_LOSS.value
        else:
            resolved = config.ExitReason.TARGET.value
        self.db.update_trade_exit(
            pos.trade_id, exit_price, pnl, resolved,
            final_stop_price=pos.stop_price,
            trail_bumps=pos.trail_bumps,
            highest_ltp=pos.highest_ltp_seen or None,
            lowest_ltp=pos.lowest_ltp_seen or None,
            exit_trigger=resolved,
            initial_sl_price=pos.initial_stop_price or None,
            initial_tp_price=pos.initial_target_price or None,
        )
        self.pnl_guard.add_realized(pnl)
        self._trades_today += 1
        self._consecutive_losses = self._consecutive_losses + 1 if pnl < 0 else 0
        logger.info(
            "Trade closed %s pnl=₹%.2f trail_bumps=%d hi=%.2f lo=%.2f "
            "final_stop=%.2f (initial=%.2f) trades_today=%d consec_loss=%d",
            resolved, pnl, pos.trail_bumps, pos.highest_ltp_seen,
            pos.lowest_ltp_seen, pos.stop_price, pos.initial_stop_price,
            self._trades_today, self._consecutive_losses,
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
                # P0-Q3: informational notes separate from weight-carrying
                # reasons. `notes` holds warm-up hints ("HTF pending — cap
                # 80") and the regime multiplier note. Dashboard should
                # render this list distinctly (e.g. as a subtle strip).
                "notes": list(result.notes or []),
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
                tg = getattr(self, "telegram", None)
                if tg is not None:
                    tg.maybe_notify_smc(payload)
            except Exception:
                logger.exception("Telegram maybe_notify_smc raised (ignored)")
            # v1.15 — Auto-trade gate. Fires the EXISTING manual-entry
            # pipeline when AUTO mode is on and threshold is met. No new
            # execution logic; just a conditional call.
            try:
                self._maybe_auto_entry(payload)
            except Exception:
                logger.exception("auto_entry gate raised (ignored)")
        except Exception:
            logger.debug("SMC scoring tick failed (continuing)", exc_info=True)

    # ─────────────────────────────────────────────── v1.15 auto trade
    def _maybe_auto_entry(self, smc_payload: dict) -> None:
        """Fire an auto-entry only when ALL gates pass. Reuses the manual
        entry code path (`_handle_manual_entry`) unchanged."""
        # 0. Sync suspension reason from the DB — the dashboard's Resume
        # button clears the DB value; the daemon picks it up here.
        try:
            row = self.db.get_state("auto_suspended_reason")
            db_reason = (row[0] if row else "") or ""
            if db_reason != (self._auto_suspended_reason or ""):
                self._auto_suspended_reason = db_reason if db_reason else None
                # v2.0.1 — clear the dedup key when the user resumes so
                # the next SMC signal is re-evaluated cleanly.
                if not db_reason:
                    self._last_auto_failure_key = None
        except Exception:
            pass
        # 1. AUTO mode must be enabled from the dashboard.
        row = self.db.get_state("auto_trade_enabled")
        if not row or str(row[0]).lower() not in ("1", "true", "yes"):
            return
        # 2. Safety-suspended → refuse until operator resumes.
        if self._auto_suspended_reason:
            return
        # 3. No existing position or pending order.
        if self.positions.has_open_position or self.positions.has_pending_entry:
            return
        # 4. FSM must be IDLE (never fire from FORCED_EXIT / SHUTDOWN).
        if self.state is not config.State.IDLE:
            return
        # 5. SMC signal must be actionable.
        direction_str = smc_payload.get("direction")
        confidence = int(smc_payload.get("confidence") or 0)
        if direction_str not in ("CALL", "PUT"):
            return
        if confidence < config.SMC_AUTO_TRADE_THRESHOLD:
            return
        # 6. Circuit breakers (single-position lock already handled above;
        # let _trip_circuit_breakers cover trade cap + ws health + PnL guard).
        breach = self._trip_circuit_breakers()
        if breach:
            return
        # 7. v2.2 — deterministic capital→lots. Always fixed mapping, no
        # more manual / auto_risk mode branching. Same computation for
        # SIM (uses sim_capital) and LIVE (uses broker capital).
        entry_ref = 0.0
        try:
            tick = self.ws.get_last_tick(smc_payload.get("token"))
            entry_ref = float(tick.ltp) if tick else 0.0
        except Exception:
            entry_ref = 0.0
        if entry_ref <= 0:
            try:
                qq = self._last_option_quote or {}
                for _tk, _q in qq.items():
                    _ltp = float(_q.get("ltp") or 0.0)
                    if _ltp > 0:
                        entry_ref = _ltp
                        break
            except Exception:
                pass
        calc = self._compute_auto_risk_lots(entry_ref)
        try:
            from execution_timeline import Event as _Ev
            session_id = getattr(self, "_timeline_session", None) or "AUTO"
            self._tl(session_id, _Ev.AUTO_SIZING,
                     f"Fixed sizing → {calc.get('final_lots')} lots "
                     f"(cap ₹{calc.get('capital')})",
                     calc)
        except Exception:
            pass
        if calc.get("error"):
            try:
                from execution_timeline import Event as _Ev
                session_id = getattr(self, "_timeline_session", None) or "AUTO"
                self._tl(session_id, _Ev.AUTO_ENTRY_CANCELLED,
                         f"AUTO cancelled — {calc['error']}", calc)
            except Exception:
                pass
            try:
                tg = getattr(self, "telegram", None)
                if tg is not None and hasattr(tg, "send_auto_cancelled"):
                    tg.send_auto_cancelled(calc["error"], calc)
            except Exception:
                pass
            return
        lots_override = calc.get("final_lots") or 2
        direction = (config.Direction.LONG if direction_str == "CALL"
                     else config.Direction.SHORT)
        reasons = list(smc_payload.get("reasons") or [])
        # v2.0.1 (Bug 2) — dedup key. Skip if we've already tried this exact
        # signal signature since the last failure. Any of these changes
        # clears the key: direction flip, confidence-bucket change (5%),
        # or a new signal timestamp. `_suspend_auto` (Bug 3) also acts as
        # a hard stop until the user Resumes.
        conf_bucket = int(confidence // 5) * 5
        sig_ts = str(smc_payload.get("timestamp") or "")
        signal_key = (direction_str, conf_bucket, sig_ts)
        if getattr(self, "_last_auto_failure_key", None) == signal_key:
            return
        logger.info("AUTO ENTRY firing: %s conf=%d%% reasons=%s",
                    direction_str, confidence, reasons[:3])
        try:
            from execution_timeline import Event as _Ev
            session = getattr(self, "_timeline_session", None) or "AUTO"
            self._tl(session, _Ev.AUTO_ENTRY,
                     f"AUTO {direction_str} @ {confidence}%",
                     {"direction": direction_str, "confidence": confidence,
                      "reasons": reasons[:5], "lots": lots_override})
        except Exception:
            pass
        ok, msg = self._handle_manual_entry(
            direction, lots_override=lots_override,
            engine="smc", confidence=confidence, reasons=reasons,
        )
        # v2.0.1 Bug 2/3 — on failure, record the signal signature so we
        # don't retry every tick, AND suspend AUTO so the operator sees a
        # dashboard banner + Telegram alert instead of endless retries.
        if not ok:
            self._last_auto_failure_key = signal_key
            ctx = self._last_reject_context or {}
            # Build a compact human-readable reason for suspension + Telegram.
            reason_text = ctx.get("user_message") or ctx.get("broker_reason") or msg or "auto entry failed"
            self._suspend_auto(reason_text)
            # v2.0.1 Bug 4 — human-readable Telegram formatter.
            try:
                tg = getattr(self, "telegram", None)
                if tg is not None and hasattr(tg, "send_auto_preflight_failed"):
                    tg.send_auto_preflight_failed(
                        direction_str, confidence, reasons,
                        ctx, contract_symbol=getattr(self, "_ce", None) and
                            (self._ce.symbol if direction_str == "CALL" and self._ce
                             else (self._pe.symbol if self._pe else "")) or "",
                        lots=lots_override or 1,
                    )
                elif tg is not None and hasattr(tg, "send_auto_entry"):
                    tg.send_auto_entry(direction_str, confidence, reasons, ok, msg)
            except Exception:
                logger.exception("Telegram auto pre-flight notify failed (ignored)")
            return
        # Success path — clear dedup so future failures/retries are fresh.
        self._last_auto_failure_key = None
        try:
            tg = getattr(self, "telegram", None)
            if tg is not None and hasattr(tg, "send_auto_entry"):
                tg.send_auto_entry(direction_str, confidence, reasons, ok, msg)
        except Exception:
            logger.exception("Telegram auto-entry notify failed (ignored)")


    # ─────────────────────────────────────────── v2.0 auto risk sizing
    def _compute_auto_risk_lots(self, entry_ref_price: float) -> dict:
        """v2.2 — deterministic capital→lots (fixed mapping).

        The v2.0 configurable risk-%/max-lots/floor-formula has been
        replaced by a single hard-coded mapping (`calculate_execution_lots`).
        Same return shape so callers, tests, and timeline events are
        unchanged. SIM uses `sim_capital`; LIVE fetches from broker; on
        failure the trade is cancelled (never silently fall back).
        """
        try:
            r = self.db.get_state("sim_capital")
            sim_capital = float(r[0]) if r else 200_000.0
        except Exception:
            sim_capital = 200_000.0
        sl_pct = float(config.MANUAL_SL_PCT)
        lot_size = int(config.LOT_SIZE_NIFTY)
        entry = float(entry_ref_price or 0.0)

        payload: dict = {
            "mode": "fixed",
            "risk_pct": FIXED_RISK_PCT,
            "sl_pct": sl_pct,
            "entry_ref": entry,
            "lot_size": lot_size,
        }

        # --- capital source (SIM never touches broker) ---
        if config.SIMULATE_ORDERS:
            capital = sim_capital
            payload["capital_source"] = "sim"
        else:
            try:
                capital = float(self.broker.get_net_available_cash())
                payload["capital_source"] = "broker"
            except Exception as exc:
                payload["error"] = f"Unable to fetch available funds: {exc}"
                payload["capital"] = 0.0
                payload["final_lots"] = 0
                return payload
        payload["capital"] = capital

        if capital <= 0:
            payload["error"] = f"Invalid capital={capital}"
            payload["final_lots"] = 0
            return payload
        if entry <= 0:
            payload["error"] = "Missing entry reference price (no live tick)"
            payload["final_lots"] = 0
            return payload

        final = calculate_execution_lots(capital)
        payload.update({
            "calculated_lots": final,
            "final_lots": final,
            "risk_amount": round(capital * FIXED_RISK_PCT / 100.0, 2),
            "loss_per_lot": round(entry * sl_pct * lot_size, 2),
        })
        return payload

    def _suspend_auto(self, reason: str) -> None:
        """Called from execution safety paths (protection failure, broker
        outage) to disable AUTO entries until the operator explicitly
        resumes from the dashboard."""
        if getattr(self, "_auto_suspended_reason", None) == reason:
            return
        self._auto_suspended_reason = reason
        try:
            self.db.set_state("auto_suspended_reason", reason)
        except Exception:
            pass
        logger.critical("AUTO SUSPENDED: %s", reason)
        try:
            from execution_timeline import Event as _Ev
            pos = self.positions.open_position
            anchor = (pos.trade_id if pos else getattr(self, "_timeline_session", None) or "AUTO")
            self._tl(anchor, _Ev.AUTO_SUSPENDED,
                     f"Auto trading suspended — {reason}",
                     {"reason": reason})
        except Exception:
            pass
        try:
            tg = getattr(self, "telegram", None)
            if tg is not None and hasattr(tg, "send_auto_suspended"):
                tg.send_auto_suspended(reason)
        except Exception:
            logger.exception("Telegram auto-suspend notify failed (ignored)")

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
        age = p.age_seconds()
        # v1.14 — early reconciliation. The v1.12 timeout branch fires only
        # at ORDER_TIMEOUT_SEC (20s), which was the exact 20-30s "still
        # ORDER_PENDING" delay reported by the user. Add an intermediate
        # attempt at ≥5s so a missed WS fill is recovered ~4× faster.
        # Runs at most once per pending (guard flag `_early_reconciled`) so
        # order_book isn't hammered every 500ms tick.
        if (
            age >= config.PENDING_EARLY_RECONCILE_SEC
            and age < config.ORDER_TIMEOUT_SEC
            and not getattr(p, "_early_reconciled", False)
        ):
            try:
                p._early_reconciled = True  # attribute stored on the dataclass instance
            except Exception:
                pass
            try:
                if self._reconcile_order_pending_timeout(p):
                    # Timeline note so operators can see the fast path fired.
                    try:
                        session = getattr(self, "_timeline_session", None)
                        pos = self.positions.open_position
                        anchor = pos.trade_id if pos and pos.trade_id else session
                        if anchor:
                            from execution_timeline import Event as _Ev
                            self._tl(
                                anchor, _Ev.BROKER_DELAY,
                                f"Recovered via early reconcile @ {age:.1f}s "
                                f"(WS fill missed)",
                                {"age_seconds": round(age, 2),
                                 "trigger": "early_reconcile"},
                            )
                    except Exception:
                        pass
                    return
            except Exception:
                logger.exception("Early reconcile raised (continuing)")
        if age >= config.ORDER_TIMEOUT_SEC:
            # v1.12 — Fix A + Fix B (execution-recovery reconciliation).
            #
            # Before firing the legacy cancel-and-go-IDLE branch, verify with
            # the broker whether the order actually filled. The WebSocket
            # `on_order(status="complete")` event is the primary source of
            # truth, but it can be missed by the SDK (reconnect straddle,
            # swallowed exception, unrecognised status string, or plain
            # delivery latency > ORDER_TIMEOUT_SEC). When that happens the
            # legacy code left an unprotected live position on the broker.
            #
            # Recovery priority (first successful source wins, others
            # become no-ops via the `pending is None` guard inside
            # `_handle_fill`):
            #    1. WebSocket fill  (fired before this branch runs)
            #    2. order_book()    — Fix A below
            #    3. positions()     — Fix B below
            # If NEITHER confirms a fill, we fall through to the original
            # cancel + IDLE behaviour so today's timeout semantics are
            # preserved.
            #
            # v1.14 — Phase X: structured attestation. The whole safety
            # chain outcome is recorded as one PENDING_TIMEOUT timeline
            # event so operators can prove per-instance that all three
            # sources were consulted before cancelling.
            attest: dict = {
                "order_id": p.order_id,
                "age_seconds": round(age, 2),
                "ws_fill_seen": False,
                "reconcile_recovered": False,
                "cancel_requested": False,
                "cancel_ok": None,
                "cancel_error": None,
                "final_state": None,
            }
            reconciled = False
            try:
                reconciled = self._reconcile_order_pending_timeout(p)
            except Exception as _rec_exc:
                attest["reconcile_error"] = str(_rec_exc)
                logger.exception("Reconcile raised at timeout (continuing to cancel)")
            attest["reconcile_recovered"] = bool(reconciled)
            if reconciled:
                attest["final_state"] = "POSITION_OPEN"
                self._emit_pending_timeout_attestation(p, attest)
                return
            # v1.14 — Phase X hard-safety: even if reconciliation returned
            # False, ask the broker one more time whether an open NFO
            # position matching our token exists. Never allow IDLE while
            # the broker still holds a live position.
            if self._broker_still_has_position(p):
                attest["broker_position_present"] = True
                attest["reconcile_recovered"] = True
                attest["final_state"] = "POSITION_OPEN (defensive adopt)"
                logger.critical(
                    "PENDING_TIMEOUT: broker still holds position for %s "
                    "after reconcile — attempting one more adoption before "
                    "refusing to go IDLE.",
                    p.contract_symbol,
                )
                # Best-effort second reconcile attempt.
                try:
                    self._reconcile_order_pending_timeout(p)
                except Exception:
                    logger.exception("Second reconcile attempt raised")
                if self.positions.pending_entry is None:
                    self._emit_pending_timeout_attestation(p, attest)
                    return
                # If we still couldn't adopt, DO NOT go IDLE — stay
                # ORDER_PENDING so a subsequent tick retries. This is
                # deliberately conservative: refusing to lose a position.
                attest["final_state"] = "ORDER_PENDING (refused IDLE)"
                self._emit_pending_timeout_attestation(p, attest)
                return
            logger.warning("Entry order %s unfilled in %ds — cancelling.",
                           p.order_id, config.ORDER_TIMEOUT_SEC)
            attest["cancel_requested"] = True
            if p.order_id and p.order_id not in (None, "None", ""):
                try:
                    self.broker.cancel_order(p.order_id)
                    attest["cancel_ok"] = True
                except Exception as _c_exc:
                    attest["cancel_ok"] = False
                    attest["cancel_error"] = str(_c_exc)
                    logger.exception("cancel_order failed (continuing)")
            self.positions.clear_pending()
            attest["final_state"] = "IDLE"
            self._emit_pending_timeout_attestation(p, attest)
            self._transition(config.State.IDLE)

    def _broker_still_has_position(self, p: "PendingEntry") -> bool:
        """Phase X safety belt: refuse to go IDLE while broker has a
        matching NFO open position. Uses positions() only; failures fall
        back to False (preserves today's behaviour)."""
        try:
            for bp in (self.broker.positions() or []):
                try:
                    if str(bp.get("exchange", "")).upper() != "NFO":
                        continue
                    if str(bp.get("symboltoken", "")) != str(p.contract_token):
                        if str(bp.get("tradingsymbol", "")).upper() != p.contract_symbol.upper():
                            continue
                    if int(float(bp.get("netqty") or 0)) != 0:
                        return True
                except Exception:
                    continue
        except Exception:
            logger.warning("_broker_still_has_position: positions() raised", exc_info=True)
        return False

    def _emit_pending_timeout_attestation(self, p: "PendingEntry", attest: dict) -> None:
        """Phase X — write the safety-chain summary as one PENDING_TIMEOUT
        timeline event under whichever anchor makes sense (trade_id if a
        position was adopted, else the pre-fill session id)."""
        try:
            from execution_timeline import Event as _Ev
            pos = self.positions.open_position
            anchor = (pos.trade_id if pos and getattr(pos, "trade_id", None)
                      else getattr(self, "_timeline_session", None))
            if anchor is None:
                return
            # Include the last few audited broker calls (order_book / positions
                # / cancel_order) so the timeline shows the raw evidence.
            recent = []
            try:
                recent = self.broker_audit.recent_by_method("order_book", 2) \
                       + self.broker_audit.recent_by_method("positions", 2) \
                       + self.broker_audit.recent_by_method("cancel_order", 1)
            except Exception:
                pass
            attest["broker_audit_tail"] = recent
            msg = (
                f"Timeout {attest['age_seconds']}s · reconciled={attest['reconcile_recovered']} · "
                f"final={attest['final_state']}"
            )
            self._tl(anchor, _Ev.PENDING_TIMEOUT, msg, attest)
        except Exception:
            logger.exception("emit_pending_timeout_attestation failed (ignored)")

    def _reconcile_order_pending_timeout(self, p: PendingEntry) -> bool:
        """v1.12 — attempt to recover a missed-WS-fill by asking the broker.

        Returns True if we successfully routed a synthetic fill through the
        existing `_handle_fill(...)` pipeline (which promotes to
        POSITION_OPEN, inserts the trade row, places SL/TP, and logs the
        timeline). Returns False when both broker sources are unable to
        prove a fill — the caller then executes the legacy cancel branch.

        Idempotency: `_handle_fill` already contains the guard
            `if pending and ev.order_id == pending.order_id:`
        so if the WebSocket fill fired between our poll and this method,
        `self.positions.pending_entry` is None and the synthesized event
        becomes a no-op. No duplicate promote, no duplicate protection, no
        duplicate trade row, no duplicate timeline events under trade_id.
        """
        # Fast idempotency guard: if the WS fill already promoted this
        # pending, there is nothing to reconcile.
        if self.positions.pending_entry is None or self.positions.has_open_position:
            return False

        session = getattr(self, "_timeline_session", None)

        # ─── Fix A: order_book() reconciliation ────────────────────────
        try:
            book = self.broker.order_book() or []
        except Exception:
            logger.warning(
                "reconcile: order_book() raised for %s — falling back to positions()",
                p.order_id, exc_info=True,
            )
            book = None

        if book is not None:
            matched = None
            for row in book:
                try:
                    if str(row.get("orderid", "")) == str(p.order_id):
                        matched = row
                        break
                except Exception:
                    continue
            if matched is not None:
                status = str(matched.get("status", "")).lower()
                if status == "complete":
                    fill_px = (
                        float(matched.get("averageprice") or 0.0)
                        or float(matched.get("avg_price") or 0.0)
                        or float(matched.get("fill_price") or 0.0)
                        or float(matched.get("price") or 0.0)
                    )
                    if fill_px > 0:
                        logger.warning(
                            "reconcile: order_book shows %s COMPLETE avg=%.2f — "
                            "recovering via _handle_fill.",
                            p.order_id, fill_px,
                        )
                        if session:
                            from execution_timeline import Event
                            self._tl(
                                session, Event.ORDER_PENDING_RECONCILE_ORDERBOOK,
                                f"order_book confirms COMPLETE avg=₹{fill_px:.2f}",
                                {"order_id": p.order_id,
                                 "broker_status": status,
                                 "avg_price": fill_px,
                                 "trigger_reason": "order_pending_timeout"},
                            )
                        ev = OrderEvent(
                            order_id=str(p.order_id),
                            status="complete",
                            fill_price=fill_px,
                            avg_price=fill_px,
                            text="reconciled from order_book at ORDER_PENDING timeout",
                            ts=time.time(),
                        )
                        self._handle_fill(ev)
                        # If _handle_fill promoted the position, we're done.
                        # (If it aborted via FORCED_EXIT — e.g. slippage guard —
                        # that is the correct behaviour and matches WS-fill
                        # semantics; still counts as reconciled.)
                        return self.positions.pending_entry is None

        # ─── Fix B: positions() reconciliation ────────────────────────
        try:
            broker_positions = self.broker.positions() or []
        except Exception:
            logger.warning(
                "reconcile: positions() raised for %s — falling through to cancel/IDLE.",
                p.order_id, exc_info=True,
            )
            return False

        # Locate an NFO position matching our pending: same token OR symbol,
        # non-zero net qty in the direction we submitted.
        expected_side = 1 if p.direction is config.Direction.LONG else -1
        matched_pos = None
        for bp in broker_positions:
            try:
                if str(bp.get("exchange", "")).upper() != "NFO":
                    continue
                token_ok = str(bp.get("symboltoken", "")) == str(p.contract_token)
                sym_ok = str(bp.get("tradingsymbol", "")).upper() == p.contract_symbol.upper()
                if not (token_ok or sym_ok):
                    continue
                net = int(float(bp.get("netqty") or 0))
                if net == 0:
                    continue
                # For options-BUY, the broker net qty is positive on LONG legs.
                if (expected_side > 0 and net > 0) or (expected_side < 0 and net < 0):
                    matched_pos = bp
                    break
            except Exception:
                continue

        if matched_pos is None:
            return False

        fill_px = (
            float(matched_pos.get("avgnetprice") or 0.0)
            or float(matched_pos.get("buyavgprice") or 0.0)
            or float(matched_pos.get("averageprice") or 0.0)
        )
        if fill_px <= 0:
            logger.warning(
                "reconcile: positions() matched %s but no usable avg price "
                "(%s) — falling through to cancel/IDLE.",
                p.contract_symbol, matched_pos,
            )
            return False

        logger.warning(
            "reconcile: positions() shows %s open netqty=%s avg=%.2f — "
            "recovering via _handle_fill.",
            p.contract_symbol,
            matched_pos.get("netqty"), fill_px,
        )
        if session:
            from execution_timeline import Event
            self._tl(
                session, Event.ORDER_PENDING_RECONCILE_POSITION,
                f"positions() confirms open netqty={matched_pos.get('netqty')} avg=₹{fill_px:.2f}",
                {"symbol": p.contract_symbol,
                 "token": p.contract_token,
                 "quantity": matched_pos.get("netqty"),
                 "avg_price": fill_px,
                 "trigger_reason": "order_pending_timeout"},
            )
        ev = OrderEvent(
            order_id=str(p.order_id),
            status="complete",
            fill_price=fill_px,
            avg_price=fill_px,
            text="reconciled from positions() at ORDER_PENDING timeout",
            ts=time.time(),
        )
        self._handle_fill(ev)
        return self.positions.pending_entry is None

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
        quote_ts = float(q.get("ts") or 0.0)

        # v1.9 P0 — Stale-quote circuit breaker
        # If we have an open position but the WS hasn't ticked this token
        # for STALE_QUOTE_EXIT_SEC seconds AND we've held it for at least
        # that long (avoid firing during the seed-only first second), force
        # a MARKET exit rather than sit unprotected. The synthetic SL/TP
        # comparators below can't fire without a live LTP — this is the
        # only path that catches a truly frozen feed. Skipped if the CB is
        # disabled (STALE_QUOTE_EXIT_SEC = 0).
        if config.STALE_QUOTE_EXIT_SEC > 0 and pos.age_seconds() >= config.STALE_QUOTE_EXIT_SEC:
            age = (time.time() - quote_ts) if quote_ts > 0 else float("inf")
            if age > config.STALE_QUOTE_EXIT_SEC:
                logger.warning(
                    "Stale-quote breaker: %s no ticks for %.1fs (threshold %ds) "
                    "→ firing STALE_FEED exit to preserve capital.",
                    pos.contract_symbol, age, config.STALE_QUOTE_EXIT_SEC,
                )
                # v1.10 timeline
                from execution_timeline import Event
                self._tl(
                    pos.trade_id, Event.STALE_FEED,
                    f"Stale quote — no ticks for {age:.0f}s → forced exit",
                    {"seconds_since_last_tick": age,
                     "threshold": config.STALE_QUOTE_EXIT_SEC},
                )
                self._exit_reason_hint = config.ExitReason.STALE_FEED.value
                self._transition(config.State.FORCED_EXIT)
                return

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

            # v1.9 telemetry — record extremes on the smoothed LTP so single
            # spike ticks don't skew the audit numbers. Updated in place on
            # the OpenPosition dataclass; persisted at close.
            if pos.highest_ltp_seen == 0.0 or ltp > pos.highest_ltp_seen:
                pos.highest_ltp_seen = ltp
            if pos.lowest_ltp_seen == 0.0 or ltp < pos.lowest_ltp_seen:
                pos.lowest_ltp_seen = ltp

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
            if new_stop is not None:
                # v1.9 P1 — persist the live position state on every trail
                # bump so a mid-trade restart resumes from the CURRENT
                # anchor, not from `entry_price`. Cheap: single INSERT OR
                # REPLACE into bot_state.
                self._persist_live_position_state()
                # v1.10 timeline — record the bump
                from execution_timeline import Event
                self._tl(
                    pos.trade_id, Event.TRAIL_BUMP,
                    f"Trail #{pos.trail_bumps} · stop moved to ₹{new_stop:.2f}",
                    {"trail_bumps": pos.trail_bumps,
                     "new_stop": new_stop,
                     "trail_anchor": pos.trail_anchor,
                     "highest_ltp_seen": pos.highest_ltp_seen,
                     "current_ltp": ltp},
                )
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
                        "triggerprice": _tick_round(new_stop),
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
                        "price": _tick_round(new_stop),
                        "triggerprice": _tick_round(new_stop * 1.001),
                        "quantity": pos.qty,
                    }
                try:
                    new_id = self.broker.place_order(new_sl_payload)
                    self.positions.set_protective_orders(pos.target_order_id, new_id)
                    logger.info(
                        "Stop trailed → ₹%.2f (%s order=%s)",
                        new_stop, config.SL_ORDER_TYPE, new_id,
                    )
                except Exception as _trail_exc:
                    # v1.14 — surface the FULL broker rejection reason via a
                    # dedicated timeline event before flattening.
                    reason = str(_trail_exc) or "broker rejected trail replace"
                    logger.critical(
                        "Trail SL re-place FAILED at ₹%.2f — position unprotected, "
                        "routing to FORCED_EXIT to flatten. Reason: %s",
                        new_stop, reason,
                    )
                    try:
                        from execution_timeline import Event as _Ev
                        self._tl(
                            pos.trade_id, _Ev.TRAIL_REJECTED,
                            f"Broker rejected trail replace: {reason}",
                            {"broker": "AngelOne", "reason": reason,
                             "new_stop": new_stop,
                             "order_type": config.SL_ORDER_TYPE,
                             "leg": "TRAIL_SL"},
                        )
                    except Exception:
                        pass
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
        """DEPRECATED — kept as a NO-OP for backward compatibility.

        Manual mid-day reset was removed per user request: once the daily
        loss cap or trade cap is hit, the bot stays shut down for the rest
        of the session. Counters reset AUTOMATICALLY at the next daily
        rollover (see `_daily_rollover_if_needed`), not by a button.
        """
        logger.warning(
            "reset_breakers() invoked but is now a no-op. "
            "Counters reset only at next-day rollover.",
        )
        return {
            "trades_today": self._trades_today,
            "consecutive_losses": self._consecutive_losses,
            "api_reject_count": self._api_reject_count,
            "state": self.state.value,
            "cancelled_commands": 0,
            "note": "reset_breakers is disabled; counters reset at next-day rollover",
        }

    def _daily_rollover_if_needed(self) -> None:
        """Auto-reset breakers when the calendar date changes in IST.

        Fires once per tick (cheap: one string compare against a cached
        session date). At the first tick of a new trading day, zeroes the
        daily counters and — if the FSM was parked in SHUTDOWN from
        yesterday's breach with no open position — flips back to IDLE so
        today's session can begin. Never touches an open position.
        """
        try:
            import pytz
            today_ist = datetime.now(pytz.timezone("Asia/Kolkata")).date().isoformat()
        except Exception:
            today_ist = datetime.now(timezone.utc).date().isoformat()
        if getattr(self, "_session_date", None) == today_ist:
            return
        # First tick of a new day (or first-ever tick after boot)
        prev = getattr(self, "_session_date", None)
        self._session_date = today_ist
        if prev is None:
            return   # boot; no rollover reset needed
        logger.warning(
            "Daily rollover %s → %s — resetting circuit-breaker counters.",
            prev, today_ist,
        )
        self._trades_today = 0
        self._consecutive_losses = 0
        self._api_reject_count = 0
        if self.state is config.State.SHUTDOWN and not self.positions.has_open_position:
            self._transition(config.State.IDLE)

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

                # P0-Q2: periodic ATM refresh REMOVED (was every 10 s in v1.7).
                # Confirmation-modal accuracy is achieved by:
                #   (a) `_handle_manual_entry` still refreshes at click time
                #       (P0-1), so the executed contract is always fresh, and
                #   (b) the frontend queues a `refresh_atm` command when the
                #       confirmation dialog opens, which the daemon drains on
                #       the next tick — bounded REST usage tied to user intent
                #       rather than a background hammer.
                self._publish_ws_health()
                self._daily_rollover_if_needed()

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
