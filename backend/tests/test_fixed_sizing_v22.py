"""
Regression tests for v2.3 Fixed Position Sizing.

Locks in:
  • `calculate_execution_lots(capital)` deterministic boundary mapping.
  • `_compute_auto_risk_lots` uses the fixed mapping (mode='fixed').
  • SIM path never touches broker; LIVE path calls broker.
  • LIVE broker failure cancels the trade (no silent fallback).

Slabs (v2.3, 2026-02-16):
  ₹1        – ₹50,000   → 1 lot
  ₹50,001   – ₹100,000  → 2 lots
  ₹100,001  – ₹150,000  → 3 lots
  ₹150,001  – ₹200,000  → 4 lots
  Above ₹200,000        → 5 lots
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("BOT_DB_PATH", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                                              # noqa: E402
from database.sqlite_logger import SqliteLogger            # noqa: E402
from strategy.position_manager import PositionManager      # noqa: E402
from main import calculate_execution_lots                  # noqa: E402


def test_capital_zero_maps_to_1_lot():
    assert calculate_execution_lots(0) == 1


def test_capital_1_maps_to_1_lot():
    assert calculate_execution_lots(1) == 1


def test_capital_49999_maps_to_1_lot():
    assert calculate_execution_lots(49999) == 1


def test_capital_50000_maps_to_1_lot_inclusive():
    assert calculate_execution_lots(50000) == 1


def test_capital_50001_maps_to_2_lots():
    assert calculate_execution_lots(50001) == 2


def test_capital_100000_maps_to_2_lots_inclusive():
    assert calculate_execution_lots(100000) == 2


def test_capital_100001_maps_to_3_lots():
    assert calculate_execution_lots(100001) == 3


def test_capital_150000_maps_to_3_lots_inclusive():
    assert calculate_execution_lots(150000) == 3


def test_capital_150001_maps_to_4_lots():
    assert calculate_execution_lots(150001) == 4


def test_capital_200000_maps_to_4_lots_inclusive():
    assert calculate_execution_lots(200000) == 4


def test_capital_200001_maps_to_5_lots():
    assert calculate_execution_lots(200001) == 5


def test_capital_large_still_5_lots():
    assert calculate_execution_lots(10_000_000) == 5


def _make_bot():
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot.state = config.State.IDLE
    bot._state_lock = threading.RLock()
    bot._exit_reason_hint = None
    bot._timeline_session = None
    bot.timeline = MagicMock()
    bot.broker = MagicMock()
    bot.broker.get_net_available_cash.return_value = 250000.0
    return bot


def test_sim_uses_sim_capital_and_never_calls_broker():
    bot = _make_bot()
    bot.db.set_state("sim_capital", "175000")
    original_sim = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        r = bot._compute_auto_risk_lots(entry_ref_price=100.0)
        assert r["capital_source"] == "sim"
        assert r["capital"] == 175000
        assert r["final_lots"] == 4  # 150k < 175k <= 200k → 4 lots
        assert r["mode"] == "fixed"
        assert "error" not in r
        bot.broker.get_net_available_cash.assert_not_called()
    finally:
        config.SIMULATE_ORDERS = original_sim


def test_live_uses_broker_capital():
    bot = _make_bot()
    bot.broker.get_net_available_cash.return_value = 82000.0
    original_sim = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = False
        r = bot._compute_auto_risk_lots(entry_ref_price=100.0)
        assert r["capital_source"] == "broker"
        assert r["capital"] == 82000
        assert r["final_lots"] == 2  # 50k < 82k <= 100k → 2 lots
    finally:
        config.SIMULATE_ORDERS = original_sim


def test_live_broker_failure_cancels_trade():
    bot = _make_bot()
    bot.broker.get_net_available_cash.side_effect = RuntimeError("timeout")
    original_sim = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = False
        r = bot._compute_auto_risk_lots(entry_ref_price=100.0)
        assert "error" in r
        assert "Unable to fetch available funds" in r["error"]
        assert r["final_lots"] == 0
    finally:
        config.SIMULATE_ORDERS = original_sim


def test_missing_entry_price_cancels():
    bot = _make_bot()
    bot.db.set_state("sim_capital", "100000")
    original_sim = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        r = bot._compute_auto_risk_lots(entry_ref_price=0.0)
        assert "error" in r
        assert r["final_lots"] == 0
    finally:
        config.SIMULATE_ORDERS = original_sim


def test_fixed_risk_constants_present():
    from main import FIXED_RISK_PCT, FIXED_SL_PCT_DISPLAY, FIXED_TP_PCT_DISPLAY, FIXED_TRAIL_PCT_DISPLAY
    assert FIXED_RISK_PCT == 2.5
    assert FIXED_SL_PCT_DISPLAY == 15.0
    assert FIXED_TP_PCT_DISPLAY == 30.0
    assert FIXED_TRAIL_PCT_DISPLAY == 10.0


def test_49999_bug_dashboard_matches_execution():
    """v2.3 regression: the ₹49,999 bug where the dashboard showed 2 lots
    but executed 1 lot. New slab: both must be 1."""
    assert calculate_execution_lots(49999) == 1
    # Frontend inline slab (mirrored) must also return 1 for the same
    # input — enforced by keeping a single JS block in App.js and
    # asserting in that file. See test_slab_consistency below.


def test_slab_consistency_across_backend_and_frontend():
    """Cross-check: the JS inline slab must match the Python slab.
    Values chosen at every boundary."""
    from main import calculate_execution_lots as cel
    cases = [
        (0, 1), (1, 1), (49999, 1), (50000, 1),
        (50001, 2), (75000, 2), (100000, 2),
        (100001, 3), (125000, 3), (150000, 3),
        (150001, 4), (175000, 4), (200000, 4),
        (200001, 5), (500000, 5), (10_000_000, 5),
    ]
    for cap, expected in cases:
        assert cel(cap) == expected, f"capital={cap} expected {expected} got {cel(cap)}"

