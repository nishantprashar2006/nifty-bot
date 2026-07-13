"""
Regression test for the AttributeError reported from VPS logs:

    AttributeError: 'NiftyOptionsBot' object has no attribute 'telegram'

Root cause was that a previous edit accidentally captured the tail of
`NiftyOptionsBot.__init__` (including `self.telegram = TelegramNotifier()`)
inside the body of the `_tl_rekey` helper, so `self.telegram` was never
set at construction time.

This test locks in the invariant: every attribute the daemon touches
BEFORE `start()` runs must exist immediately after `NiftyOptionsBot()`.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from main import NiftyOptionsBot  # noqa: E402
from notifications.telegram import TelegramNotifier  # noqa: E402


def test_bot_has_telegram_attribute_after_construction():
    """`self.telegram` must be set inside __init__, not lazily."""
    bot = NiftyOptionsBot()
    assert hasattr(bot, "telegram"), (
        "NiftyOptionsBot.__init__ must assign self.telegram; missing attribute "
        "reproduces the VPS AttributeError."
    )
    assert isinstance(bot.telegram, TelegramNotifier)


def test_bot_startup_ping_does_not_crash_when_disabled():
    """send_startup() must be safely callable straight after construction."""
    bot = NiftyOptionsBot()
    # Force-disable to avoid any network I/O in the test environment.
    bot.telegram.enabled = False
    # Must not raise — `send_startup` should short-circuit when disabled.
    bot.telegram.send_startup()


def test_bot_has_all_init_tail_attributes():
    """Guard the specific attributes that were previously misplaced inside
    `_tl_rekey`. Any regression that moves them out of __init__ will fail
    here rather than at runtime on the VPS."""
    bot = NiftyOptionsBot()
    for name in (
        "telegram",
        "_exit_reason_hint",
        "_spread_history",
        "_ltp_history",
        "timeline",
        "_timeline_session",
    ):
        assert hasattr(bot, name), f"NiftyOptionsBot missing '{name}' after __init__"
