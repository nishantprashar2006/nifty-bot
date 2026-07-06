"""tests/test_telegram_notifier.py
Unit tests for the Telegram notification module. Verifies the dedup +
threshold contract without ever hitting the network."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from notifications.telegram import TelegramNotifier


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    """Ensure Telegram is 'enabled' with fake creds so the notifier proceeds
    past the enabled-check. Every _send call is stubbed to return True."""
    monkeypatch.setattr(config, "TELEGRAM_ENABLED", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setattr(config, "SMC_ALERT_THRESHOLD", 40)


def _fake_notifier() -> TelegramNotifier:
    n = TelegramNotifier()
    n._send = MagicMock(return_value=True)   # short-circuit network
    return n


def _payload(direction: str, confidence: int, **extra) -> dict:
    p = {
        "direction": direction,
        "confidence": confidence,
        "grade": "B",
        "htf_trend": "CALL" if direction == "CALL" else "PUT",
        "market_structure": "CALL" if direction == "CALL" else "PUT",
        "regime": "TRENDING",
        "reasons": ["test"],
        "timestamp": "10:00:00",
        "entry": 100.0, "stop_loss": 90.0, "target": 120.0,
    }
    p.update(extra)
    return p


# ─────────────────────────────────────────── disabled state
def test_disabled_notifier_is_no_op(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ENABLED", False)
    n = TelegramNotifier()
    n._send = MagicMock(return_value=True)
    n.maybe_notify_smc(_payload("CALL", 90))
    n.send_startup()
    n._send.assert_not_called()


def test_missing_token_disables(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    n = TelegramNotifier()
    assert n.enabled is False


# ─────────────────────────────────────────── threshold gate
def test_below_threshold_is_ignored():
    n = _fake_notifier()
    n.maybe_notify_smc(_payload("CALL", 39))
    n._send.assert_not_called()


def test_at_and_above_threshold_fires():
    n = _fake_notifier()
    n.maybe_notify_smc(_payload("CALL", 40))
    n._send.assert_called_once()


def test_neutral_direction_is_ignored():
    """SMC.direction=NEUTRAL should never trigger an alert regardless of
    confidence — nothing to trade on."""
    n = _fake_notifier()
    n.maybe_notify_smc(_payload("NEUTRAL", 90))
    n._send.assert_not_called()


# ─────────────────────────────────────────── dedup contract (per spec)
def test_dedup_identical_signal_no_second_alert():
    n = _fake_notifier()
    n.maybe_notify_smc(_payload("CALL", 41))
    n.maybe_notify_smc(_payload("CALL", 41))   # identical
    assert n._send.call_count == 1


def test_confidence_change_fires_new_alert():
    n = _fake_notifier()
    n.maybe_notify_smc(_payload("CALL", 41))
    n.maybe_notify_smc(_payload("CALL", 42))   # confidence changed
    assert n._send.call_count == 2


def test_direction_flip_fires_new_alert():
    n = _fake_notifier()
    n.maybe_notify_smc(_payload("CALL", 45))
    n.maybe_notify_smc(_payload("PUT", 44))    # side flipped
    assert n._send.call_count == 2


def test_ping_pong_alerts_every_time_direction_flips():
    n = _fake_notifier()
    n.maybe_notify_smc(_payload("CALL", 50))
    n.maybe_notify_smc(_payload("PUT",  50))
    n.maybe_notify_smc(_payload("CALL", 50))
    assert n._send.call_count == 3


def test_alert_below_threshold_does_not_clear_dedup_state():
    """A drop below threshold should NOT reset the last-alert memory.
    If confidence recovers back to the SAME value+direction, no new alert."""
    n = _fake_notifier()
    n.maybe_notify_smc(_payload("CALL", 60))    # sends
    n.maybe_notify_smc(_payload("CALL", 30))    # below threshold — no send
    n.maybe_notify_smc(_payload("CALL", 60))    # identical to last SENT — no send
    assert n._send.call_count == 1


# ─────────────────────────────────────────── startup ping
def test_startup_fires_exactly_once():
    n = _fake_notifier()
    n.send_startup()
    n.send_startup()
    n.send_startup()
    assert n._send.call_count == 1


# ─────────────────────────────────────────── failure safety
def test_send_failure_does_not_advance_dedup_state():
    """If the HTTP send fails, the last-alert memory must NOT be updated —
    otherwise a network hiccup would silently swallow a real alert."""
    n = TelegramNotifier()
    n._send = MagicMock(return_value=False)   # simulate telegram error
    n.maybe_notify_smc(_payload("CALL", 50))
    assert n._last_alert is None
    # A subsequent identical payload should STILL attempt to send
    n.maybe_notify_smc(_payload("CALL", 50))
    assert n._send.call_count == 2


# ─────────────────────────────────────────── format sanity
def test_message_contains_required_fields():
    n = _fake_notifier()
    text = n._format(_payload("CALL", 87, grade="A+"), "CALL", 87)
    assert "BUY CALL" in text
    assert "87%" in text
    assert "A+" in text
    assert "Bullish" in text
    assert "Entry" in text
