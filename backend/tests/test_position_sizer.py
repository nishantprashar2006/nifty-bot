"""Unit tests for risk.position_sizer (math only — no broker)."""
from __future__ import annotations

import os
import tempfile

import pytest

# Ensure config picks up a temp DB path BEFORE importing modules that read it
os.environ.setdefault("BOT_DB_PATH", tempfile.mktemp(suffix=".db"))

import config  # noqa: E402
from database.sqlite_logger import SqliteLogger  # noqa: E402
from risk.position_sizer import PositionSizer  # noqa: E402


@pytest.fixture()
def sizer(tmp_path):
    db = SqliteLogger(str(tmp_path / "test.db"))
    return PositionSizer(db), db


def test_base_lots_floors_to_min(sizer):
    s, _ = sizer
    assert s.compute_base_lots(10_000) == config.MIN_LOTS  # below 50k → min


def test_base_lots_grows_with_capital(sizer):
    s, _ = sizer
    assert s.compute_base_lots(150_000) == 3
    assert s.compute_base_lots(600_000) == 12  # but will be capped later


def test_scale_multiplier_matrix(sizer):
    s, _ = sizer
    assert s._scale_for(0.0) == 1.0
    assert s._scale_for(0.05) == 1.0
    assert s._scale_for(0.10) == 0.75
    assert s._scale_for(0.20) == 0.50
    assert s._scale_for(0.31) == 0.0  # floor → handled separately


def test_full_sizing_no_drawdown(sizer):
    s, _ = sizer
    res = s.update_equity_and_size(current_equity=200_000)
    assert res.effective_lots == min(config.MAX_LOTS_DYNAMIC, 4)
    assert res.drawdown_pct == 0.0
    assert res.daily_loss_cap == -config.LOSS_PER_LOT * res.effective_lots
    # P0-7: profit lock removed — the SizingResult no longer carries one.
    assert not hasattr(res, "daily_profit_lock")


def test_drawdown_forces_floor(sizer):
    s, db = sizer
    db.log_equity_point(300_000, 300_000, 0.0, 5, trading_mode=config.TRADING_MODE)
    res = s.update_equity_and_size(current_equity=200_000)  # 33% drawdown
    assert res.effective_lots == 1
    assert res.drawdown_pct >= 0.30


def test_max_lots_cap_enforced(sizer):
    s, _ = sizer
    res = s.update_equity_and_size(current_equity=2_000_000)  # base_lots=40
    assert res.effective_lots <= config.MAX_LOTS_DYNAMIC


def test_premium_spike_guard_reduces_lots(sizer):
    s, _ = sizer
    # Premium so high that 1 lot already eats >25% of ₹200k
    lots = s.premium_spike_guard(
        option_premium=10_000, effective_lots=8, current_equity=200_000
    )
    assert lots == 1


def test_premium_spike_guard_keeps_lots_when_safe(sizer):
    s, _ = sizer
    lots = s.premium_spike_guard(
        option_premium=80, effective_lots=4, current_equity=200_000
    )
    assert lots == 4


def test_atr_stops_match_multipliers():
    sl, tp = PositionSizer.stops_from_atr(option_atr=10.0)
    assert sl == config.ATR_SL_MULT * 10.0
    assert tp == config.ATR_TP_MULT * 10.0
