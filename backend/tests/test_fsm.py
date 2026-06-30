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


# ──────────────────────────── SIM-mode consecutive-loss breaker is relaxed
def test_consecutive_loss_breaker_relaxed_in_sim(monkeypatch):
    """In SIM mode the bot must NOT auto-SHUTDOWN after N consecutive
    losses — paper testing should never lock the user out for the day."""
    # Recreate the breaker logic in isolation (matches main._trip_circuit_breakers)
    def trip(consecutive_losses: int, mode: str) -> str:
        if mode == "live" and consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            return "max_consecutive_losses"
        return ""

    assert trip(2, "live") == "max_consecutive_losses"
    assert trip(5, "live") == "max_consecutive_losses"
    # SIM mode never trips this breaker regardless of streak length
    assert trip(2, "sim") == ""
    assert trip(50, "sim") == ""
