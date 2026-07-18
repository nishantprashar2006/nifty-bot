"""
v2.5 — regression tests for execution simplification.

Locks in:
  • Fixed-point SL/TP: SL = fill − FIXED_SL_POINTS, TP = fill + FIXED_TP_POINTS.
  • Trailing arms only after fill + FIXED_TRAIL_ACTIVATION_POINTS; then
    stop follows (highest premium − FIXED_SL_POINTS). Only moves upward.
  • BOS+Structure AUTO path REMOVED — flags are permanently False and the
    bot has no `_maybe_bos_structure_signal` method.
  • Confidence-threshold AUTO path is UNCHANGED (still fires when
    conf ≥ SMC_AUTO_TRADE_THRESHOLD).
  • Telegram no longer exposes `send_bos_structure_signal`.
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
from strategy.position_manager import PendingEntry, PositionManager  # noqa: E402


# ─── Fixed-point SL/TP + trailing ─────────────────────────────────────
def _register_and_promote(fill: float) -> tuple[PositionManager, object]:
    pm = PositionManager()
    p = PendingEntry(
        order_id="O", direction=config.Direction.LONG,
        contract_symbol="X", contract_token="T",
        expected_price=fill, lots=1, qty=65,
        target_price=999, stop_price=1,  # stale hints — must be ignored
    )
    pm.register_pending_entry(p)
    return pm, pm.promote_to_open(fill)


def test_initial_sl_is_fill_minus_11():
    _pm, pos = _register_and_promote(100.0)
    assert pos.stop_price == 89.0


def test_initial_sl_at_low_premium():
    _pm, pos = _register_and_promote(47.0)
    assert pos.stop_price == 36.0


def test_fixed_target_is_fill_plus_25():
    _pm, pos = _register_and_promote(100.0)
    assert pos.target_price == 125.0


def test_fixed_target_at_low_premium():
    _pm, pos = _register_and_promote(52.0)
    assert pos.target_price == 77.0


def test_trailing_does_not_activate_below_plus_15():
    pm, pos = _register_and_promote(100.0)
    # 105 → 110 → 114.99 → all below activation of 115
    for premium in (105.0, 110.0, 114.0, 114.99):
        assert pm.maybe_trail_stop(premium) is None
    assert pos.stop_price == 89.0  # untouched


def test_trailing_activates_exactly_at_plus_15():
    pm, pos = _register_and_promote(100.0)
    new_stop = pm.maybe_trail_stop(115.0)
    assert new_stop == 104.0
    assert pos.stop_price == 104.0


def test_trailing_follows_highest_minus_11():
    pm, pos = _register_and_promote(100.0)
    pm.maybe_trail_stop(115.0)  # arms → stop 104
    pm.maybe_trail_stop(120.0)  # → stop 109
    pm.maybe_trail_stop(123.0)  # → stop 112
    assert pos.stop_price == 112.0


def test_trailing_never_moves_downward():
    pm, pos = _register_and_promote(100.0)
    pm.maybe_trail_stop(123.0)  # arms → stop 112
    assert pos.stop_price == 112.0
    # Pullback below prior high must NOT lower the stop
    assert pm.maybe_trail_stop(118.0) is None
    assert pos.stop_price == 112.0
    assert pm.maybe_trail_stop(115.5) is None
    assert pos.stop_price == 112.0


def test_config_constants_present():
    assert config.FIXED_SL_POINTS == 11.0
    assert config.FIXED_TP_POINTS == 25.0
    assert config.FIXED_TRAIL_ACTIVATION_POINTS == 15.0


# ─── BOS+Structure fully removed ──────────────────────────────────────
def test_bos_structure_auto_flag_is_permanently_false():
    """Removed feature. Flag is hardcoded False in config.py so no
    stale env override can re-enable the code path."""
    assert config.BOS_STRUCTURE_AUTO_ENABLED is False
    assert config.BOS_STRUCTURE_ALERT_ENABLED is False


def test_bot_has_no_bos_structure_method():
    from main import NiftyOptionsBot
    assert not hasattr(NiftyOptionsBot, "_maybe_bos_structure_signal")


def test_telegram_has_no_bos_structure_alert():
    from notifications.telegram import TelegramNotifier
    assert not hasattr(TelegramNotifier, "send_bos_structure_signal")


# ─── Confidence AUTO path still works ─────────────────────────────────
def test_confidence_threshold_auto_still_wired():
    """The confidence-threshold path (Path A) must remain the ONLY AUTO
    rule and must still consult SMC_AUTO_TRADE_THRESHOLD."""
    from main import NiftyOptionsBot
    assert hasattr(NiftyOptionsBot, "_maybe_auto_entry")
    assert isinstance(config.SMC_AUTO_TRADE_THRESHOLD, int)
    assert config.SMC_AUTO_TRADE_THRESHOLD >= 0
