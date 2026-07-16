"""
v2.3 Phase 2 — regression tests for the BOS + Structure dual-trigger.

Locks in:
  • Alignment condition: bos ∈ {CALL, PUT} AND market_structure == bos
  • Alert fires ONCE per (direction, bars_5m) — no spam within a candle
  • AUTO trade fires ONCE per (direction, bars_5m), independent of the
    confidence-threshold path
  • Confidence threshold path is UNTOUCHED — the two paths coexist
  • BOS+Structure obeys AUTO toggle, suspension, single-position lock
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
    bot.telegram = MagicMock()
    bot.ws = MagicMock()
    bot.ws.get_last_tick.return_value = MagicMock(ltp=100.0)
    bot._last_option_quote = {}
    bot.broker = MagicMock()
    bot.broker.get_net_available_cash.return_value = 200000.0
    bot._auto_suspended_reason = None
    bot._last_auto_failure_key = None
    bot._last_bos_structure_key = None
    bot._last_bos_structure_alert_key = None
    bot._handle_manual_entry = MagicMock(return_value=(True, "ok"))
    bot._trip_circuit_breakers = MagicMock(return_value=None)
    return bot


def _payload(direction: str, bos: str, structure: str, confidence: int = 15, bars_5m: int = 40):
    return {
        "direction": direction, "confidence": confidence,
        "bos": bos, "market_structure": structure,
        "htf_trend": "NEUTRAL", "regime": "TRENDING",
        "reasons": ["BOS confirmed", "HH+HL live"],
        "bars_5m": bars_5m, "bars_15m": 30,
        "timestamp": "10:35:00",
    }


# ─── ALIGNMENT ────────────────────────────────────────────────────────
def test_bullish_bos_plus_hh_hl_fires_call():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot.db.set_state("sim_capital", "100000")  # → 2 lots
    original = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        bot._maybe_bos_structure_signal(_payload("CALL", "CALL", "CALL"))
    finally:
        config.SIMULATE_ORDERS = original
    bot._handle_manual_entry.assert_called_once()
    call = bot._handle_manual_entry.call_args
    assert call.args[0] == config.Direction.LONG
    assert call.kwargs["engine"] == "smc_bos_struct"
    # Telegram advisory must ALSO fire — informational alert
    bot.telegram.send_bos_structure_signal.assert_called_once()


def test_bearish_bos_plus_lh_ll_fires_put():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot.db.set_state("sim_capital", "100000")
    original = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        bot._maybe_bos_structure_signal(_payload("PUT", "PUT", "PUT"))
    finally:
        config.SIMULATE_ORDERS = original
    bot._handle_manual_entry.assert_called_once()
    assert bot._handle_manual_entry.call_args.args[0] == config.Direction.SHORT


def test_bos_call_but_structure_put_does_not_fire():
    """Misaligned: BOS Bullish but Structure = LH+LL. Must NOT fire."""
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot._maybe_bos_structure_signal(_payload("CALL", "CALL", "PUT"))
    bot._handle_manual_entry.assert_not_called()
    bot.telegram.send_bos_structure_signal.assert_not_called()


def test_bos_none_does_not_fire():
    """No BOS yet — never fire on structure alone."""
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    p = _payload("CALL", "CALL", "CALL")
    p["bos"] = None
    bot._maybe_bos_structure_signal(p)
    bot._handle_manual_entry.assert_not_called()


def test_structure_neutral_does_not_fire():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    p = _payload("CALL", "CALL", "NEUTRAL")
    bot._maybe_bos_structure_signal(p)
    bot._handle_manual_entry.assert_not_called()


# ─── DEDUP ────────────────────────────────────────────────────────────
def test_same_5m_candle_only_fires_once():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot.db.set_state("sim_capital", "100000")
    original = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        p = _payload("CALL", "CALL", "CALL", bars_5m=40)
        bot._maybe_bos_structure_signal(p)
        bot._maybe_bos_structure_signal(p)
        bot._maybe_bos_structure_signal(p)
    finally:
        config.SIMULATE_ORDERS = original
    assert bot._handle_manual_entry.call_count == 1
    assert bot.telegram.send_bos_structure_signal.call_count == 1


def test_new_5m_candle_fires_again():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot.db.set_state("sim_capital", "100000")
    original = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        bot._maybe_bos_structure_signal(_payload("CALL", "CALL", "CALL", bars_5m=40))
        # New 5m candle formed — bars_5m increments — should re-arm
        bot.positions = PositionManager()  # simulate flat again
        bot._maybe_bos_structure_signal(_payload("CALL", "CALL", "CALL", bars_5m=41))
    finally:
        config.SIMULATE_ORDERS = original
    assert bot._handle_manual_entry.call_count == 2
    assert bot.telegram.send_bos_structure_signal.call_count == 2


# ─── GATES ────────────────────────────────────────────────────────────
def test_auto_disabled_blocks_trade_but_alert_still_fires():
    """User's spec: Telegram alert is informational (always). AUTO trade
    only fires when AUTO mode is on. This split must be respected."""
    bot = _make_bot()
    # auto_trade_enabled NOT set → default false
    bot.db.set_state("sim_capital", "100000")
    bot._maybe_bos_structure_signal(_payload("CALL", "CALL", "CALL"))
    bot._handle_manual_entry.assert_not_called()
    bot.telegram.send_bos_structure_signal.assert_called_once()


def test_auto_suspended_blocks_trade():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot._auto_suspended_reason = "manual halt"
    bot._maybe_bos_structure_signal(_payload("CALL", "CALL", "CALL"))
    bot._handle_manual_entry.assert_not_called()


def test_open_position_blocks_trade():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    # Simulate open position via MagicMock override
    fake_positions = MagicMock()
    fake_positions.has_open_position = True
    fake_positions.has_pending_entry = False
    bot.positions = fake_positions
    bot._maybe_bos_structure_signal(_payload("CALL", "CALL", "CALL"))
    bot._handle_manual_entry.assert_not_called()


def test_ignores_confidence_threshold():
    """Whole point: confidence=5 (below any threshold) must still fire."""
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot.db.set_state("sim_capital", "100000")
    original = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        bot._maybe_bos_structure_signal(_payload("CALL", "CALL", "CALL", confidence=5))
    finally:
        config.SIMULATE_ORDERS = original
    bot._handle_manual_entry.assert_called_once()
    assert bot._handle_manual_entry.call_args.kwargs["confidence"] == 5


def test_flag_disabled_blocks_trade_but_not_alert():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot.db.set_state("sim_capital", "100000")
    original_flag = config.BOS_STRUCTURE_AUTO_ENABLED
    try:
        config.BOS_STRUCTURE_AUTO_ENABLED = False
        bot._maybe_bos_structure_signal(_payload("CALL", "CALL", "CALL"))
    finally:
        config.BOS_STRUCTURE_AUTO_ENABLED = original_flag
    bot._handle_manual_entry.assert_not_called()
    bot.telegram.send_bos_structure_signal.assert_called_once()
