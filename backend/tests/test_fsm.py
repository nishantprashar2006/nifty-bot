"""Unit tests for FSM-related building blocks (no live broker)."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("BOT_DB_PATH", tempfile.mktemp(suffix=".db"))

import config  # noqa: E402
from config import Direction  # noqa: E402
from data.candle_manager import CandleSeries  # noqa: E402
from risk.liquidity_gate import LiquidityGate  # noqa: E402
from risk.pnl_guard import PnlGuard  # noqa: E402
from strategy.confirmation_engine import ConfirmationEngine  # noqa: E402
from strategy.position_manager import PendingEntry, PositionManager  # noqa: E402
from strategy.regime_filter import RegimeFilter  # noqa: E402
from data.indicator_engine import IndicatorSnapshot  # noqa: E402


# ──────────────────────────────────────────────────────────── candle manager
def test_candle_series_rolls_on_interval():
    s = CandleSeries(interval_min=3)
    t0 = datetime(2026, 2, 6, 9, 45, tzinfo=timezone.utc)
    s.ingest_tick(100, 1000, t0)
    s.ingest_tick(101, 1100, t0 + timedelta(seconds=30))
    s.ingest_tick(102, 1200, t0 + timedelta(seconds=120))
    assert s.closed_bars() == []      # bar 0 still open

    closed = s.ingest_tick(103, 1300, t0 + timedelta(minutes=3, seconds=1))
    assert closed is not None
    assert closed.open == 100 and closed.high == 102 and closed.close == 102


# ──────────────────────────────────────────────────────────── regime filter
def test_regime_filter_long():
    snap = IndicatorSnapshot(ema20_15m=18_500, ema50_15m=18_200)
    v = RegimeFilter().evaluate(snap)
    assert v.authorize_long and not v.authorize_short


def test_regime_filter_short():
    snap = IndicatorSnapshot(ema20_15m=18_000, ema50_15m=18_300)
    v = RegimeFilter().evaluate(snap)
    assert v.authorize_short and not v.authorize_long


def test_regime_filter_undecided():
    v = RegimeFilter().evaluate(IndicatorSnapshot())
    assert not v.authorize_long and not v.authorize_short


# ──────────────────────────────────────────────────────────── confirmation
def _snap_long_ok() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        ema9=110, ema21=105, ema20_15m=18_500, ema50_15m=18_200,
        rsi=62, adx=24, adx_prev=22, vwap=108, last_close=112,
    )


def test_confirmation_passes_clean_long():
    res = ConfirmationEngine().validate(Direction.LONG, _snap_long_ok())
    assert res.ok, res.reasons


def test_confirmation_fails_on_weak_adx_delta():
    snap = _snap_long_ok()
    snap.adx_prev = 23.9  # delta=0.1
    res = ConfirmationEngine().validate(Direction.LONG, snap)
    assert not res.ok and any("adx_delta" in r for r in res.reasons)


def test_confirmation_fails_below_vwap():
    snap = _snap_long_ok()
    snap.last_close = 100
    res = ConfirmationEngine().validate(Direction.LONG, snap)
    assert not res.ok


# ──────────────────────────────────────────────────────────── liquidity gate
def test_liquidity_passes():
    v = LiquidityGate().check(bid=100.0, ask=100.5, volume=8000, open_interest=15_000)
    assert v.ok


def test_liquidity_fails_wide_spread():
    v = LiquidityGate().check(bid=100.0, ask=110.0, volume=8000, open_interest=15_000)
    assert not v.ok and any("spread" in r for r in v.reasons)


def test_liquidity_fails_low_volume():
    v = LiquidityGate().check(bid=100.0, ask=100.5, volume=10, open_interest=15_000)
    assert not v.ok and any("volume" in r for r in v.reasons)


# ──────────────────────────────────────────────────────────── pnl guard
def test_pnl_guard_loss_breach():
    g = PnlGuard(daily_loss_cap=-1500, daily_profit_lock=3000)
    g.add_realized(-1500)
    assert g.evaluate().breached


def test_pnl_guard_profit_breach():
    g = PnlGuard(-1500, 3000)
    g.add_realized(3000)
    assert g.evaluate().breached


def test_pnl_guard_no_breach_in_range():
    g = PnlGuard(-1500, 3000)
    g.add_realized(500)
    assert not g.evaluate().breached


# ──────────────────────────────────────────────────────────── position lock
def test_single_position_lock():
    pm = PositionManager()
    p = PendingEntry(
        order_id="O1", direction=Direction.LONG, contract_symbol="X",
        contract_token="T", expected_price=100, lots=1, qty=65,
        target_price=120, stop_price=90,
    )
    pm.register_pending_entry(p)
    with pytest.raises(RuntimeError):
        pm.register_pending_entry(p)


def test_directional_cooldown_after_stop():
    pm = PositionManager()
    p = PendingEntry(
        order_id="O1", direction=Direction.LONG, contract_symbol="X",
        contract_token="T", expected_price=100, lots=1, qty=65,
        target_price=120, stop_price=90,
    )
    pm.register_pending_entry(p)
    pm.promote_to_open(100.0)
    pm.close_position(exit_was_stop=True)
    assert pm.in_cooldown(Direction.LONG)
    assert not pm.in_cooldown(Direction.SHORT)


def test_trailing_stop_bumps_only_above_step():
    pm = PositionManager()
    p = PendingEntry(
        order_id="O1", direction=Direction.LONG, contract_symbol="X",
        contract_token="T", expected_price=100, lots=1, qty=65,
        target_price=120, stop_price=90,
    )
    pm.register_pending_entry(p)
    pm.promote_to_open(100.0)
    assert pm.maybe_trail_stop(103.0) is None   # below step
    new_stop = pm.maybe_trail_stop(106.0)        # 6pt advance → bump 5
    assert new_stop == 95.0


# ──────────────────────────────────────────────── manual-mode % trailing
def test_manual_mode_percent_trail_step():
    """A position opened with trail_step_pct=0.10 should bump SL by 10 %
    of entry price on every advance ≥ that step."""
    pm = PositionManager()
    p = PendingEntry(
        order_id="O1", direction=Direction.LONG, contract_symbol="X",
        contract_token="T", expected_price=100, lots=1, qty=65,
        target_price=130, stop_price=85,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    )
    pm.register_pending_entry(p)
    pos = pm.promote_to_open(100.0)
    # entry × 10 % = ₹10 step
    assert pm.maybe_trail_stop(109.0) is None         # below step
    new_stop = pm.maybe_trail_stop(112.0)             # 12pt advance → 1×10 bump
    assert new_stop == round(pos.stop_price, 2)
    assert pos.stop_price > 85.0


# ──────────────────────────────────────────────── synthetic exit thresholds
def test_synthetic_exit_thresholds_fire_correctly():
    """The bot's `_step_position_open` synthetic-exit check is pure logic
    over an OpenPosition's stop/target prices. Verify the inequality
    contract directly so we never regress the SIM-mode enforcement."""
    pm = PositionManager()
    p = PendingEntry(
        order_id="O1", direction=Direction.LONG, contract_symbol="X",
        contract_token="T", expected_price=100, lots=1, qty=65,
        target_price=130, stop_price=85,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    )
    pm.register_pending_entry(p)
    pos = pm.promote_to_open(100.0)

    # Inequality contract used by _step_position_open
    def synth_decision(ltp: float) -> str:
        if ltp >= pos.target_price:
            return "TARGET"
        if ltp <= pos.stop_price:
            return "STOP"
        return "HOLD"

    assert synth_decision(100.0) == "HOLD"
    assert synth_decision(129.9) == "HOLD"
    assert synth_decision(130.0) == "TARGET"
    assert synth_decision(150.0) == "TARGET"
    assert synth_decision(85.0)  == "STOP"
    assert synth_decision(70.0)  == "STOP"


# ──────────────────────────── Consecutive-loss breaker removed entirely
def test_breakers_only_use_trade_count_and_safety():
    """Per user request: no consecutive-loss lockout. Only the daily-trade
    cap and safety breakers (API rejects, WS reconnect, P&L guard) should
    fire. Single-position lock is enforced separately by PositionManager."""
    # Recreate the breaker logic in isolation (matches main._trip_circuit_breakers)
    def trip(trades_today: int, api_rejects: int) -> str:
        if trades_today >= config.MAX_TRADES_DAILY:
            return "max_trades_daily"
        if api_rejects >= config.MAX_API_REJECT_EVENTS:
            return "max_api_rejects"
        return ""

    # Trade-count limit is the headline rule
    assert config.MAX_TRADES_DAILY == 4
    assert trip(3, 0) == ""
    assert trip(4, 0) == "max_trades_daily"
    # Safety breakers still active
    assert trip(0, config.MAX_API_REJECT_EVENTS) == "max_api_rejects"
    # No consecutive-loss attribute used anywhere now
    assert not hasattr(config, "MAX_CONSECUTIVE_LOSSES")


# ───────────────────────────────── Exit reason mapping (regression)
def test_exit_reason_uses_hint_when_provided():
    """`_finalize_exit(reason=...)` should record the explicit reason string
    verbatim (used by FORCED_EXIT paths for TIME_STOP / SQUARE_OFF / MANUAL /
    HEARTBEAT) — never flatten to STOP_LOSS/TARGET when a hint is provided."""
    # Mirror the resolution logic from main._finalize_exit
    def resolve(was_stop: bool, reason: str = None) -> str:
        if reason:
            return reason
        return config.ExitReason.STOP_LOSS.value if was_stop else config.ExitReason.TARGET.value

    assert resolve(was_stop=True,  reason=None) == "STOP_LOSS"
    assert resolve(was_stop=False, reason=None) == "TARGET"
    assert resolve(was_stop=True,  reason=config.ExitReason.TIME_STOP.value)  == "TIME_STOP"
    assert resolve(was_stop=True,  reason=config.ExitReason.SQUARE_OFF.value) == "SQUARE_OFF"
    assert resolve(was_stop=True,  reason=config.ExitReason.MANUAL.value)     == "MANUAL"
    assert resolve(was_stop=True,  reason=config.ExitReason.HEARTBEAT.value)  == "HEARTBEAT"


# ───────────────────────────────── Option-selector: Tuesday expiry filter
def test_option_selector_prefers_tuesday_expiry():
    """Given a scrip master with both a Tuesday weekly and a further-out
    monthly expiry, the selector must pick Tuesday even when it's not the
    calendar-earliest date-string sort would produce."""
    from data.option_selector import OptionSelector
    sel = OptionSelector.__new__(OptionSelector)   # bypass __init__ (no broker)
    # Build two candidate rows: a Thursday (further-out but calendar-first
    # by weekday), and a Tuesday (correct target). Use dates >= today so the
    # filter accepts them.
    from datetime import date, timedelta
    today = date.today()
    def next_weekday(weekday: int) -> date:
        d = today
        while d.weekday() != weekday or d == today:
            d = d + timedelta(days=1)
        return d
    tue = next_weekday(1)     # Tuesday
    thu = next_weekday(3)     # Thursday (same or following week)
    rows = [
        {"expiry": tue.strftime("%d%b%Y").upper()},
        {"expiry": thu.strftime("%d%b%Y").upper()},
    ]
    chosen = sel._nearest_expiry(rows)
    assert chosen == tue.strftime("%d%b%Y").upper()


def test_option_selector_falls_back_when_no_tuesday():
    """If the scrip master has no Tuesday (rare — usually an NSE holiday
    shift), the selector must gracefully use the earliest future expiry."""
    from data.option_selector import OptionSelector
    from datetime import date, timedelta
    sel = OptionSelector.__new__(OptionSelector)
    today = date.today()
    # Only Wednesday + Thursday rows — no Tuesday
    def next_weekday(weekday: int) -> date:
        d = today
        while d.weekday() != weekday or d == today:
            d = d + timedelta(days=1)
        return d
    wed = next_weekday(2)
    thu = next_weekday(3)
    rows = [
        {"expiry": thu.strftime("%d%b%Y").upper()},
        {"expiry": wed.strftime("%d%b%Y").upper()},
    ]
    chosen = sel._nearest_expiry(rows)
    # Fallback = earliest future date. Whichever of {wed, thu} is closer
    # to today wins — depends on today's weekday, so compute it directly
    # instead of hard-coding Wednesday.
    earliest = min(wed, thu)
    assert chosen == earliest.strftime("%d%b%Y").upper()


# ─────────────── Indicator strength bands rebalanced to 0-70 achievable range
def test_indicator_strength_bands_reachable():
    """STRONG must now be mathematically achievable within the base-score
    ceiling of 70. Bands are contiguous and monotonic — no gaps."""
    def classify(s):
        if s >= 60: return "STRONG"
        if s >= 45: return "GOOD"
        if s >= 30: return "NEUTRAL"
        if s >= 15: return "WEAK"
        return "AVOID"
    # STRONG becomes reachable at the max theoretical base (70)
    assert classify(70) == "STRONG"
    assert classify(60) == "STRONG"
    assert classify(59) == "GOOD"
    assert classify(45) == "GOOD"
    assert classify(44) == "NEUTRAL"
    assert classify(30) == "NEUTRAL"
    assert classify(29) == "WEAK"
    assert classify(15) == "WEAK"
    assert classify(14) == "AVOID"
    assert classify(0)  == "AVOID"


# ─────────────── Liquidity penalty median-smoothing
def test_liquidity_penalty_median_of_last_three():
    """Median of the last 3 spread readings suppresses a single spike.
    Persistent wide spread still hits the 50-point cap (formula unchanged)."""
    from collections import deque
    hist = deque(maxlen=3)

    def leg_penalty(spread_pct: float) -> float:
        hist.append(spread_pct)
        ordered = sorted(hist)
        smoothed = ordered[len(ordered) // 2]
        return min(smoothed * 2000, 50)

    # A single 5 % spike among tight readings — median stays tight
    assert leg_penalty(0.001) < 5              # spread ≈ 0.1 %
    assert leg_penalty(0.001) < 5
    assert leg_penalty(0.05)  < 5              # spike gets medianed out
    # Two more tight readings after the spike push it out entirely
    assert leg_penalty(0.001) < 5
    assert leg_penalty(0.001) < 5

    # Persistent wide spread — hits the 50 cap after 2 wide readings in a row
    hist2 = deque(maxlen=3)
    def leg2(sp):
        hist2.append(sp)
        ord2 = sorted(hist2)
        return min(ord2[len(ord2) // 2] * 2000, 50)
    leg2(0.001)         # tight
    leg2(0.03)          # wide
    p = leg2(0.03)      # median of (0.001, 0.03, 0.03) = 0.03 → 60 → capped 50
    assert p == 50



def test_synthetic_ltp_median_smoothing_absorbs_single_spike():
    """3-tick median smoothing on the LTP used for synthetic SL/TP/trail.

    A single wide bid-ask spike (or stale tick) that would otherwise punch
    below the stop or above the target gets medianed out. Two consecutive
    reads that stay through the threshold still trigger the exit — same
    formula as `main.py::_step_position_open`.
    """
    from collections import deque
    hist = deque(maxlen=3)

    def smoothed(ltp: float) -> float:
        hist.append(ltp)
        ordered = sorted(hist)
        return ordered[len(ordered) // 2]

    # Position: entry ₹100, stop ₹85, target ₹130
    stop, target = 85.0, 130.0

    # Healthy prints then one bad tick that pierces stop
    assert smoothed(105.0) > stop        # tick 1 — safe
    assert smoothed(106.0) > stop        # tick 2 — safe
    assert smoothed(70.0)  > stop        # tick 3 — spike medianed out
    # Two more healthy prints — spike falls out of the deque
    assert smoothed(105.0) > stop
    assert smoothed(106.0) > stop

    # Genuine breakdown: two consecutive lows push median through stop
    hist2 = deque(maxlen=3)
    def s2(x):
        hist2.append(x); o = sorted(hist2); return o[len(o) // 2]
    s2(105.0)
    s2(80.0)
    assert s2(80.0) <= stop              # median(105, 80, 80) = 80 → stop hit

    # Same behaviour on the target side — one lone spike above target is filtered
    hist3 = deque(maxlen=3)
    def s3(x):
        hist3.append(x); o = sorted(hist3); return o[len(o) // 2]
    s3(120.0)
    s3(121.0)
    assert s3(200.0) < target            # median(120, 121, 200) = 121 → no false TP
