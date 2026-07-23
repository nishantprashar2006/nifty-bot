"""
v3.0 — regression tests for execution layer simplification.

Locks in:
  • Fixed-point SL/TP FLOORED to whole ₹:
      SL = floor(fill − FIXED_SL_POINTS)   (default 6)
      TP = floor(fill + FIXED_TP_POINTS)   (default 12)
  • Trailing stop REMOVED — `maybe_trail_stop` is a permanent no-op.
  • Entry threshold DEFAULT lowered to 10 % (`SMC_ENTRY_THRESHOLD`).
  • Reversal threshold `SMC_REVERSAL_THRESHOLD` = 25 %.
  • Cooldown DISABLED (`REENTRY_BLOCK_MIN == 0`).
  • ExitReason.REVERSAL exists.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

os.environ.setdefault("BOT_DB_PATH", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                                              # noqa: E402
from strategy.position_manager import PendingEntry, PositionManager  # noqa: E402


# ─── SL/TP floor rounding ─────────────────────────────────────────────
def _register_and_promote(fill: float):
    pm = PositionManager()
    p = PendingEntry(
        order_id="O", direction=config.Direction.LONG,
        contract_symbol="X", contract_token="T",
        expected_price=fill, lots=1, qty=65,
        target_price=999, stop_price=1,  # stale hints — must be ignored
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,  # legacy — ignored
    )
    pm.register_pending_entry(p)
    return pm, pm.promote_to_open(fill)


@pytest.mark.parametrize("fill,expected_sl,expected_tp", [
    # Example from spec
    (100.25, 94, 112),
    # Boundary — no fractional part
    (100.00, 94, 112),
    # High premium
    (247.85, 241, 259),
    # Low premium — SL floor safety clamps to 1 minimum
    (10.00, 4, 22),
    # Fill just above SL clamp
    (7.00, 1, 19),
    # Very high fill
    (500.99, 494, 512),
])
def test_sl_tp_are_floored_whole_numbers(fill, expected_sl, expected_tp):
    _pm, pos = _register_and_promote(fill)
    assert pos.stop_price == expected_sl, f"fill={fill} expected SL={expected_sl} got {pos.stop_price}"
    assert pos.target_price == expected_tp, f"fill={fill} expected TP={expected_tp} got {pos.target_price}"


def test_sl_never_goes_below_one_rupee():
    """Defensive clamp — SL orders must be positive."""
    _pm, pos = _register_and_promote(3.0)
    assert pos.stop_price >= 1.0


# ─── Trailing removed ─────────────────────────────────────────────────
def test_maybe_trail_stop_is_noop_regardless_of_premium():
    pm, pos = _register_and_promote(100.0)
    initial_stop = pos.stop_price
    for premium in (100.0, 105.0, 115.0, 125.0, 200.0, 90.0, 50.0):
        assert pm.maybe_trail_stop(premium) is None
        assert pos.stop_price == initial_stop
        assert pos.trail_bumps == 0


# ─── Config sanity ────────────────────────────────────────────────────
def test_config_v30_defaults():
    assert config.FIXED_SL_POINTS == 6.0
    assert config.FIXED_TP_POINTS == 12.0
    assert config.SMC_ENTRY_THRESHOLD == 10
    assert config.SMC_REVERSAL_THRESHOLD == 25
    assert config.REENTRY_BLOCK_MIN == 0


def test_exit_reason_reversal_exists():
    assert config.ExitReason.REVERSAL.value == "REVERSAL"


# ─── Reversal wiring on the bot ───────────────────────────────────────
def _make_bot_with_position(direction_str: str):
    """Build a bot mid-trade with an open position in `direction_str`."""
    from main import NiftyOptionsBot
    from unittest.mock import MagicMock

    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = MagicMock()
    bot.db.get_state = MagicMock(side_effect=lambda k: {
        "auto_trade_enabled": ("true",),
    }.get(k))
    bot.state = config.State.POSITION_OPEN
    bot._state_lock = threading.RLock()
    bot._auto_suspended_reason = None
    bot._last_auto_failure_key = None
    bot._last_reversal_key = None
    bot._exit_reason_hint = None
    bot._timeline_session = None
    bot.timeline = MagicMock()
    bot._tl = MagicMock()
    bot._transition = MagicMock()
    bot._trip_circuit_breakers = MagicMock(return_value=None)

    # Register a pending, promote to open with the requested direction
    d = (config.Direction.LONG if direction_str == "CALL" else config.Direction.SHORT)
    p = PendingEntry(
        order_id="O", direction=d,
        contract_symbol="X", contract_token="T",
        expected_price=100.0, lots=1, qty=65,
        target_price=112, stop_price=94,
    )
    bot.positions.register_pending_entry(p)
    bot.positions.promote_to_open(100.0)
    return bot


def _payload(direction: str, confidence: int, gen_at: str = "ABC-1"):
    return {
        "direction": direction, "confidence": confidence,
        "generated_at": gen_at, "timestamp": "10:30:00",
    }


def test_reversal_fires_when_opposite_signal_above_threshold():
    bot = _make_bot_with_position("CALL")
    bot._maybe_auto_entry(_payload("PUT", 31))
    assert bot._transition.called
    assert bot._transition.call_args.args[0] is config.State.FORCED_EXIT
    assert bot._exit_reason_hint == "REVERSAL"


def test_reversal_ignored_when_same_direction():
    bot = _make_bot_with_position("CALL")
    bot._maybe_auto_entry(_payload("CALL", 80))
    bot._transition.assert_not_called()


def test_reversal_ignored_when_confidence_below_25():
    bot = _make_bot_with_position("CALL")
    bot._maybe_auto_entry(_payload("PUT", 24))
    bot._transition.assert_not_called()


def test_reversal_ignored_when_confidence_exactly_24():
    """Threshold is inclusive at 25 — anything below must NOT fire."""
    bot = _make_bot_with_position("CALL")
    bot._maybe_auto_entry(_payload("PUT", 24))
    bot._transition.assert_not_called()


def test_reversal_fires_at_exact_25_threshold():
    bot = _make_bot_with_position("CALL")
    bot._maybe_auto_entry(_payload("PUT", 25))
    bot._transition.assert_called_once()


def test_reversal_dedups_same_signal_within_candle():
    """Two ticks with the same generated_at must fire at most once."""
    bot = _make_bot_with_position("CALL")
    bot._maybe_auto_entry(_payload("PUT", 40, gen_at="SIG-1"))
    bot._maybe_auto_entry(_payload("PUT", 40, gen_at="SIG-1"))
    bot._maybe_auto_entry(_payload("PUT", 40, gen_at="SIG-1"))
    assert bot._transition.call_count == 1


def test_reversal_re_arms_on_new_signal():
    bot = _make_bot_with_position("CALL")
    bot._maybe_auto_entry(_payload("PUT", 40, gen_at="SIG-1"))
    # Simulate a new completed-5m signal (different generated_at)
    bot._maybe_auto_entry(_payload("PUT", 40, gen_at="SIG-2"))
    assert bot._transition.call_count == 2


# ─── Entry threshold ──────────────────────────────────────────────────
def test_no_entry_below_10_percent_confidence():
    from main import NiftyOptionsBot
    from unittest.mock import MagicMock
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = MagicMock()
    bot.db.get_state = MagicMock(side_effect=lambda k: {
        "auto_trade_enabled": ("true",),
    }.get(k))
    bot.state = config.State.IDLE
    bot._state_lock = threading.RLock()
    bot._auto_suspended_reason = None
    bot._last_auto_failure_key = None
    bot._last_reversal_key = None
    bot._exit_reason_hint = None
    bot._timeline_session = None
    bot.timeline = MagicMock()
    bot._tl = MagicMock()
    bot._transition = MagicMock()
    bot._trip_circuit_breakers = MagicMock(return_value=None)
    bot._handle_manual_entry = MagicMock()

    bot._maybe_auto_entry({"direction": "CALL", "confidence": 9})
    bot._handle_manual_entry.assert_not_called()
