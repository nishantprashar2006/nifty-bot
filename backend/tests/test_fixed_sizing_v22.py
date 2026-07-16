"""
Regression tests for v2.2 Fixed Position Sizing.

Locks in:
  • `calculate_execution_lots(capital)` deterministic boundary mapping.
  • `_compute_auto_risk_lots` uses the fixed mapping (mode='fixed').
  • SIM path never touches broker; LIVE path calls broker.
  • LIVE broker failure cancels the trade (no silent fallback).
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


def test_capital_zero_maps_to_2_lots():
    assert calculate_execution_lots(0) == 2


def test_capital_49999_maps_to_2_lots():
    assert calculate_execution_lots(49999) == 2


def test_capital_50000_maps_to_3_lots():
    assert calculate_execution_lots(50000) == 3


def test_capital_79999_maps_to_3_lots():
    assert calculate_execution_lots(79999) == 3


def test_capital_80000_maps_to_4_lots():
    assert calculate_execution_lots(80000) == 4


def test_capital_149999_maps_to_4_lots():
    assert calculate_execution_lots(149999) == 4


def test_capital_150000_maps_to_5_lots():
    assert calculate_execution_lots(150000) == 5


def test_capital_199999_maps_to_5_lots():
    assert calculate_execution_lots(199999) == 5


def test_capital_200000_maps_to_6_lots():
    assert calculate_execution_lots(200000) == 6


def test_capital_large_still_6_lots():
    assert calculate_execution_lots(10_000_000) == 6


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
        assert r["final_lots"] == 5  # 150k <= 175k < 200k → 5 lots
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
        assert r["final_lots"] == 4  # 80k <= 82k < 150k → 4 lots
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
