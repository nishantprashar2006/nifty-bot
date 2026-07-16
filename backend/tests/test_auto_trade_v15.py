"""
Regression tests for v1.15 SIM auto-trading feature.

Locks in:
  • `_maybe_auto_entry` no-ops when AUTO mode is off.
  • Fires when AUTO on, confidence >= threshold, no open position, IDLE.
  • Refuses when suspended.
  • Reuses `_handle_manual_entry` — same execution pipeline.
  • Suspension flag is set on protection failure and persisted.
  • API endpoints toggle mode and lots without restart.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    bot._auto_suspended_reason = None
    bot._last_auto_failure_key = None
    bot._exit_reason_hint = None
    bot._timeline_session = None
    bot.timeline = MagicMock()
    bot._handle_manual_entry = MagicMock(return_value=(True, "ok"))
    bot._trip_circuit_breakers = MagicMock(return_value=None)
    bot.telegram = MagicMock()
    bot.telegram.enabled = False
    bot._tl = MagicMock()
    # v2.2 — sizer requires a live tick for the fixed capital→lots calc.
    bot.broker = MagicMock()
    bot.broker.get_net_available_cash.return_value = 200000.0
    bot.ws = MagicMock()
    bot.ws.get_last_tick.return_value = MagicMock(ltp=100.0, ts=0.0)
    bot._last_option_quote = {"55555": {"ltp": 100.0}}
    return bot


def _payload(direction="CALL", confidence=55, reasons=None):
    return {
        "direction": direction,
        "confidence": confidence,
        "reasons": reasons or ["Bull BOS", "Bull OB retest"],
    }


def test_auto_entry_noop_when_disabled():
    bot = _make_bot()
    # DB has no auto_trade_enabled row → default off.
    bot._maybe_auto_entry(_payload())
    bot._handle_manual_entry.assert_not_called()


def test_auto_entry_fires_when_enabled_and_gates_pass():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot.db.set_state("sim_capital", "200000")   # → 6 lots via fixed mapping
    # Force SIM mode so sim_capital is used.
    original_sim = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        bot._maybe_auto_entry(_payload("PUT", 47))
    finally:
        config.SIMULATE_ORDERS = original_sim
    bot._handle_manual_entry.assert_called_once()
    call = bot._handle_manual_entry.call_args
    assert call.args[0] == config.Direction.SHORT
    # v2.2 — capital ≥ 200k → 6 lots (fixed mapping).
    assert call.kwargs["lots_override"] == 6
    assert call.kwargs["engine"] == "smc"
    assert call.kwargs["confidence"] == 47


def test_auto_entry_blocked_when_confidence_below_threshold():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    threshold = config.SMC_AUTO_TRADE_THRESHOLD
    bot._maybe_auto_entry(_payload("CALL", threshold - 1))
    bot._handle_manual_entry.assert_not_called()


def test_auto_entry_blocked_when_position_open():
    from strategy.position_manager import OpenPosition
    from datetime import datetime, timezone
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot.positions.adopt_open_position(OpenPosition(
        trade_id="T", direction=config.Direction.LONG,
        contract_symbol="X", contract_token="1",
        entry_price=10, qty=65, lots=1,
        entry_ts=datetime.now(timezone.utc),
        target_price=13, stop_price=8,
    ))
    bot._maybe_auto_entry(_payload("CALL", 80))
    bot._handle_manual_entry.assert_not_called()


def test_auto_entry_blocked_when_suspended():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot.db.set_state("auto_suspended_reason", "protection failed")
    bot._maybe_auto_entry(_payload("CALL", 80))
    bot._handle_manual_entry.assert_not_called()
    assert bot._auto_suspended_reason == "protection failed"


def test_auto_entry_syncs_suspension_from_db():
    """Operator's dashboard 'Resume' writes empty string; daemon must
    clear its in-memory flag on the next SMC tick."""
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot._auto_suspended_reason = "old reason"
    bot.db.set_state("auto_suspended_reason", "")  # dashboard-cleared
    bot._maybe_auto_entry(_payload("CALL", 80))
    # Since suspension was cleared, entry should fire.
    bot._handle_manual_entry.assert_called_once()
    assert bot._auto_suspended_reason is None


def test_auto_entry_blocked_when_circuit_breaker_tripped():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot._trip_circuit_breakers.return_value = "max_trades_daily"
    bot._maybe_auto_entry(_payload("CALL", 80))
    bot._handle_manual_entry.assert_not_called()


def test_auto_entry_direction_neutral_is_noop():
    bot = _make_bot()
    bot.db.set_state("auto_trade_enabled", "true")
    bot._maybe_auto_entry(_payload("NEUTRAL", 80))
    bot._handle_manual_entry.assert_not_called()


def test_suspend_auto_sets_reason_and_persists_to_db():
    from execution_timeline import Event
    bot = _make_bot()
    bot._suspend_auto("Protection incomplete: TP=X · SL=Y")
    assert bot._auto_suspended_reason.startswith("Protection incomplete")
    row = bot.db.get_state("auto_suspended_reason")
    assert row is not None and row[0].startswith("Protection incomplete")
    # Timeline event was emitted.
    calls = [c for c in bot._tl.call_args_list if c.args[1] == Event.AUTO_SUSPENDED]
    assert len(calls) == 1


def test_suspend_auto_is_idempotent():
    bot = _make_bot()
    bot._suspend_auto("Reason A")
    bot._suspend_auto("Reason A")  # duplicate — must not re-fire
    calls = bot._tl.call_args_list
    from execution_timeline import Event
    matched = [c for c in calls if c.args[1] == Event.AUTO_SUSPENDED]
    assert len(matched) == 1


# ─── API endpoint tests ────────────────────────────────────────────
def test_api_auto_mode_toggle_persists_state():
    from server import set_auto_mode, AutoModeRequest, _read_bot_state
    # Enable
    r = set_auto_mode(AutoModeRequest(enabled=True))
    assert r["auto_trade_enabled"] is True
    assert r["mode"] == "AUTO"
    assert _read_bot_state("auto_trade_enabled") == "true"
    # Enabling should clear any lingering suspension.
    assert _read_bot_state("auto_suspended_reason", "sentinel") == ""
    # Disable
    r2 = set_auto_mode(AutoModeRequest(enabled=False))
    assert r2["mode"] == "MANUAL"
    assert _read_bot_state("auto_trade_enabled") == "false"


def test_api_default_lots_persistence():
    from server import set_default_lots, DefaultLotsRequest, _read_bot_state
    r = set_default_lots(DefaultLotsRequest(lots=3))
    assert r["default_lots"] == 3
    assert _read_bot_state("default_lots") == "3"


def test_api_auto_resume_clears_suspension():
    from server import auto_resume, _read_bot_state, _write_bot_state
    _write_bot_state("auto_suspended_reason", "boom")
    r = auto_resume()
    assert r["auto_suspended_reason"] is None
    assert _read_bot_state("auto_suspended_reason") == ""
