"""
v2.3 Phase 4 — regression tests for intraday-only candle lifecycle.

Locks in:
  • `CandleManager.reset_intraday()` clears bars + working bar for every
    registered (token, interval) series (registrations preserved).
  • `_load_intraday_history` skips when before market open, skips when
    after 15:15 IST, and seeds today's bars when within market hours.
  • `_maybe_eod_clear` fires at most once per date after 15:15 IST and
    wipes CandleManager + SMC in-memory state.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("BOT_DB_PATH", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                                              # noqa: E402
from database.sqlite_logger import SqliteLogger            # noqa: E402
from data.candle_manager import Bar, CandleManager         # noqa: E402


# ─── CandleManager.reset_intraday ─────────────────────────────────────
def test_reset_intraday_clears_all_series_bars():
    cm = CandleManager()
    now = datetime.now(timezone.utc)
    s3 = cm.series("TOK1", 3)
    s3.bars.append(Bar(ts=now, open=1, high=2, low=1, close=2, volume=10))
    s15 = cm.series("TOK1", 15)
    s15.bars.append(Bar(ts=now, open=1, high=2, low=1, close=2, volume=10))
    assert len(s3.bars) == 1 and len(s15.bars) == 1

    cm.reset_intraday()
    assert len(s3.bars) == 0
    assert len(s15.bars) == 0
    # Series registrations must be preserved (listeners stay wired)
    assert cm.series("TOK1", 3) is s3
    assert cm.series("TOK1", 15) is s15


def test_reset_intraday_clears_working_bar():
    cm = CandleManager()
    s = cm.series("TOK1", 5)
    s.ingest_tick(100.0, 0)
    assert s.working_bar() is not None
    cm.reset_intraday()
    assert s.working_bar() is None


# ─── _load_intraday_history + _maybe_eod_clear ────────────────────────
def _make_bot_for_lifecycle():
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot.candles = CandleManager()
    bot.broker = MagicMock()
    bot.state = config.State.IDLE
    bot._state_lock = threading.RLock()
    bot._smc_signal = None
    bot._last_bos_structure_key = None
    bot._last_bos_structure_alert_key = None
    bot._last_auto_failure_key = None
    bot._pending_signal = None
    bot._eod_cleared_for_date = None
    return bot


def _ist_dt(hh: int, mm: int) -> datetime:
    """A tz-aware datetime whose IST hour/minute is hh:mm today."""
    from main import IST_OFFSET
    # We construct a UTC datetime such that (utc + 5:30) == today at hh:mm
    now_ist = datetime.now(timezone.utc) + IST_OFFSET
    target_ist = now_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (target_ist - IST_OFFSET).replace(tzinfo=timezone.utc)


def test_load_intraday_history_skips_before_market_open():
    bot = _make_bot_for_lifecycle()
    with patch("main._now_ist", return_value=(datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).replace(hour=8, minute=0)):
        bot._load_intraday_history()
    bot.broker.get_candles.assert_not_called()


def test_load_intraday_history_skips_after_eod():
    bot = _make_bot_for_lifecycle()
    with patch("main._now_ist", return_value=(datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).replace(hour=16, minute=0)):
        bot._load_intraday_history()
    bot.broker.get_candles.assert_not_called()


def test_load_intraday_history_seeds_bars_within_market_hours():
    bot = _make_bot_for_lifecycle()

    # Angel returns rows: [ts_iso, o, h, l, c, v]
    def _fake_rows(exchange, symboltoken, interval, from_ts, to_ts):
        return [
            ["2026-02-16T09:15:00+05:30", 24000.0, 24010.0, 23995.0, 24005.0, 100],
            ["2026-02-16T09:20:00+05:30", 24005.0, 24020.0, 24000.0, 24015.0, 120],
        ]
    bot.broker.get_candles.side_effect = _fake_rows
    bot._update_smc_score = MagicMock()

    now_utc = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).replace(hour=11, minute=0) - timedelta(hours=5, minutes=30)
    with patch("main._now_ist", return_value=now_utc + timedelta(hours=5, minutes=30)):
        bot._load_intraday_history()

    # 3 intervals (3m/5m/15m) each get their get_candles call
    assert bot.broker.get_candles.call_count == 3
    # Series should be seeded
    assert len(bot.candles.series(config.NIFTY_SPOT_TOKEN, 5).closed_bars()) == 2
    # SMC refresh must be triggered exactly once after seeding
    bot._update_smc_score.assert_called_once()


def test_maybe_eod_clear_fires_after_1515_ist_once_per_day():
    bot = _make_bot_for_lifecycle()
    # Populate some state to prove it gets wiped.
    bot.candles.series(config.NIFTY_SPOT_TOKEN, 5).bars.append(
        Bar(ts=datetime.now(timezone.utc), open=1, high=2, low=1, close=2, volume=1)
    )
    bot._smc_signal = {"direction": "CALL", "confidence": 50}
    bot._last_bos_structure_key = ("CALL", 42)

    from main import IST_OFFSET
    after_1515 = (datetime.now(timezone.utc) + IST_OFFSET).replace(hour=15, minute=20)
    with patch("main._now_ist", return_value=after_1515):
        bot._maybe_eod_clear()
    # First call wipes state
    assert bot._smc_signal is None
    assert bot._last_bos_structure_key is None
    assert len(bot.candles.series(config.NIFTY_SPOT_TOKEN, 5).closed_bars()) == 0
    # Second call same day is a no-op — set some state again to prove idempotence
    bot._smc_signal = {"direction": "PUT", "confidence": 70}
    with patch("main._now_ist", return_value=after_1515):
        bot._maybe_eod_clear()
    # Should NOT be wiped because it's the same date (already cleared once)
    assert bot._smc_signal is not None


def test_maybe_eod_clear_skips_before_1515():
    bot = _make_bot_for_lifecycle()
    bot._smc_signal = {"direction": "CALL", "confidence": 50}

    from main import IST_OFFSET
    before_1515 = (datetime.now(timezone.utc) + IST_OFFSET).replace(hour=13, minute=0)
    with patch("main._now_ist", return_value=before_1515):
        bot._maybe_eod_clear()
    # Nothing wiped — outside EOD window
    assert bot._smc_signal is not None
    assert bot._eod_cleared_for_date is None
