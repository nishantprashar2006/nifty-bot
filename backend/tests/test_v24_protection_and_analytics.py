"""
v2.4 — regression tests for Daily Loss Protection, Trigger Persistence,
Trigger Stats, and EOD Summary.

Locks in:
  • Daily loss cap = capital × risk% / 100 (uses realized PnL only).
  • Realized loss ≥ cap → AUTO suspends with reason MAX_DAILY_LOSS,
    persists `daily_loss_hit` payload, calls Telegram.
  • Idempotent — a second call within the same day is a no-op.
  • `_daily_rollover_if_needed` auto-clears MAX_DAILY_LOSS the next day.
  • `trigger_reason` propagates: MANUAL, CONFIDENCE_THRESHOLD, BOS_STRUCTURE.
  • DB helpers `realized_pnl_today` and `trigger_stats` are correct.
  • `_maybe_send_eod_summary` fires exactly once per day after 15:10 IST.
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


def _fresh_db():
    return SqliteLogger(tempfile.mktemp(suffix=".db"))


def _insert_closed(db: SqliteLogger, trade_id: str, pnl: float,
                    trigger: str, exit_time_utc: str, confidence: int = 40):
    db.insert_trade_entry(
        trade_id=trade_id, direction="CALL", qty=65,
        entry_price=100.0, entry_time=exit_time_utc,
        source="auto", engine="smc", confidence=confidence,
        trigger_reason=trigger,
    )
    db.update_trade_exit(
        trade_id=trade_id, exit_price=100 + pnl / 65,
        pnl=pnl, exit_reason="TARGET" if pnl >= 0 else "STOP_LOSS",
        exit_time=exit_time_utc,
    )


# ─── DB helpers ───────────────────────────────────────────────────────
def test_realized_pnl_today_sums_only_todays_closed_trades():
    db = _fresh_db()
    today_ist = "2026-02-16"
    # Today (IST 12:00 → UTC 06:30)
    _insert_closed(db, "T1", 1000, "CONFIDENCE_THRESHOLD", "2026-02-16T06:30:00")
    _insert_closed(db, "T2", -500, "BOS_STRUCTURE", "2026-02-16T07:00:00")
    # Yesterday
    _insert_closed(db, "T3", 999, "MANUAL", "2026-02-15T08:00:00")
    assert db.realized_pnl_today(today_ist) == 500.0


def test_realized_pnl_today_excludes_open_trades():
    db = _fresh_db()
    # Open trade — no exit_time
    db.insert_trade_entry(
        trade_id="OPEN1", direction="CALL", qty=65, entry_price=100,
        source="auto", trigger_reason="MANUAL",
    )
    assert db.realized_pnl_today("2026-02-16") == 0.0


def test_trigger_stats_groups_by_trigger_reason():
    db = _fresh_db()
    _insert_closed(db, "A1", 1000, "CONFIDENCE_THRESHOLD", "2026-02-16T06:00:00")
    _insert_closed(db, "A2", -500, "CONFIDENCE_THRESHOLD", "2026-02-16T07:00:00")
    _insert_closed(db, "B1", 750, "BOS_STRUCTURE", "2026-02-16T08:00:00")
    _insert_closed(db, "M1", -100, "MANUAL", "2026-02-16T09:00:00")
    stats = db.trigger_stats("2026-02-16")
    by = {r["trigger"]: r for r in stats}
    assert by["CONFIDENCE_THRESHOLD"]["trades"] == 2
    assert by["CONFIDENCE_THRESHOLD"]["wins"] == 1
    assert by["CONFIDENCE_THRESHOLD"]["losses"] == 1
    assert by["CONFIDENCE_THRESHOLD"]["net_pnl"] == 500.0
    assert by["BOS_STRUCTURE"]["net_pnl"] == 750.0
    assert by["MANUAL"]["net_pnl"] == -100.0


# ─── Daily loss protection (bot side) ─────────────────────────────────
def _make_bot():
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.db = _fresh_db()
    bot.telegram = MagicMock()
    bot.timeline = MagicMock()
    bot.broker = MagicMock()
    bot.state = config.State.IDLE
    bot._state_lock = threading.RLock()
    bot._auto_suspended_reason = None
    return bot


def test_max_daily_loss_computed_from_capital_and_risk_pct():
    bot = _make_bot()
    bot.db.set_state("sim_capital", "100000")
    bot.db.set_state("risk_pct", "2.5")
    original = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        assert bot._max_daily_loss() == 2500.0
    finally:
        config.SIMULATE_ORDERS = original


def test_check_daily_loss_limit_triggers_suspension():
    bot = _make_bot()
    bot.db.set_state("sim_capital", "100000")
    bot.db.set_state("risk_pct", "2.5")
    today_ist = datetime.now(timezone.utc).replace(tzinfo=timezone.utc)
    # Add closed loss trades totalling -3000 (cap is 2500)
    ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()
    _insert_closed(bot.db, "L1", -1500, "MANUAL",
                    (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
    _insert_closed(bot.db, "L2", -1500, "MANUAL",
                    (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    original = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        bot._check_daily_loss_limit()
    finally:
        config.SIMULATE_ORDERS = original
    assert bot._auto_suspended_reason == "MAX_DAILY_LOSS"
    bot.telegram.send_daily_loss_hit.assert_called_once()
    row = bot.db.get_state("daily_loss_hit")
    assert row is not None
    import json as _json
    payload = _json.loads(row[0])
    assert payload["max_loss"] == 2500.0
    assert payload["realized_pnl"] == -3000.0


def test_check_daily_loss_limit_no_op_when_under_cap():
    bot = _make_bot()
    bot.db.set_state("sim_capital", "100000")
    bot.db.set_state("risk_pct", "2.5")
    _insert_closed(bot.db, "L1", -1000, "MANUAL",
                    (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    original = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        bot._check_daily_loss_limit()
    finally:
        config.SIMULATE_ORDERS = original
    assert bot._auto_suspended_reason is None
    bot.telegram.send_daily_loss_hit.assert_not_called()


def test_check_daily_loss_idempotent_after_suspension():
    bot = _make_bot()
    bot.db.set_state("sim_capital", "100000")
    bot.db.set_state("risk_pct", "2.5")
    _insert_closed(bot.db, "L1", -3000, "MANUAL",
                    (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    original = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        bot._check_daily_loss_limit()
        bot._check_daily_loss_limit()
        bot._check_daily_loss_limit()
    finally:
        config.SIMULATE_ORDERS = original
    assert bot.telegram.send_daily_loss_hit.call_count == 1


def test_rollover_clears_max_daily_loss():
    from main import NiftyOptionsBot
    bot = _make_bot()
    bot.positions = MagicMock()
    bot.positions.has_open_position = False
    bot._auto_suspended_reason = "MAX_DAILY_LOSS"
    bot.db.set_state("auto_suspended_reason", "MAX_DAILY_LOSS")
    bot.db.set_state("daily_loss_hit", '{"a":1}')
    bot._session_date = "2026-02-15"
    bot._trades_today = 0
    bot._consecutive_losses = 0
    bot._api_reject_count = 0
    with patch("main.datetime") as mock_dt:
        # Force new IST date
        mock_dt.now.return_value = datetime(2026, 2, 16, 5, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        bot._daily_rollover_if_needed()
    assert bot._auto_suspended_reason is None


# ─── Trigger propagation ──────────────────────────────────────────────
def test_manual_entry_trigger_normalisation():
    """`_handle_manual_entry` must normalise the trigger label to one
    of the three canonical values."""
    from main import NiftyOptionsBot
    from strategy.position_manager import PositionManager
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = _fresh_db()
    bot.state = config.State.IDLE
    bot._state_lock = threading.RLock()
    bot._exit_reason_hint = None
    bot._timeline_session = None
    bot.timeline = MagicMock()
    bot._in_entry_window = lambda: True
    bot._trip_circuit_breakers = MagicMock(return_value=None)
    bot._last_reject_context = None
    bot._refresh_atm_contracts = lambda: None
    bot._ce = MagicMock(); bot._pe = MagicMock()
    bot._place_entry = MagicMock(return_value=True)
    bot._pending_source = None

    # Unknown label → normalised to MANUAL
    bot._handle_manual_entry(config.Direction.LONG, trigger="something_weird")
    assert bot._pending_trigger == "MANUAL"

    # Valid label preserved
    bot._pending_trigger = None
    bot.positions = PositionManager()
    bot._handle_manual_entry(config.Direction.LONG, trigger="BOS_STRUCTURE")
    assert bot._pending_trigger == "BOS_STRUCTURE"


# ─── EOD summary ──────────────────────────────────────────────────────
def test_eod_summary_fires_once_after_1510_ist():
    bot = _make_bot()
    bot._auto_suspended_reason = None
    from main import IST_OFFSET
    after_1510 = (datetime.now(timezone.utc) + IST_OFFSET).replace(hour=15, minute=12)
    with patch("main._now_ist", return_value=after_1510):
        bot._maybe_send_eod_summary()
        bot._maybe_send_eod_summary()
        bot._maybe_send_eod_summary()
    assert bot.telegram.send_eod_summary.call_count == 1


def test_eod_summary_skipped_before_1510():
    bot = _make_bot()
    from main import IST_OFFSET
    before_1510 = (datetime.now(timezone.utc) + IST_OFFSET).replace(hour=14, minute=59)
    with patch("main._now_ist", return_value=before_1510):
        bot._maybe_send_eod_summary()
    bot.telegram.send_eod_summary.assert_not_called()


def test_eod_summary_includes_per_trigger_breakdown():
    bot = _make_bot()
    _insert_closed(bot.db, "S1", 2300, "CONFIDENCE_THRESHOLD",
                    (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    _insert_closed(bot.db, "S2", 1650, "BOS_STRUCTURE",
                    (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    _insert_closed(bot.db, "S3", 300, "MANUAL",
                    (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())

    from main import IST_OFFSET
    after_1510 = (datetime.now(timezone.utc) + IST_OFFSET).replace(hour=15, minute=15)
    with patch("main._now_ist", return_value=after_1510):
        bot._maybe_send_eod_summary()

    call = bot.telegram.send_eod_summary.call_args
    summary = call.args[0]
    assert summary["trades"] == 3
    assert summary["realized_pnl"] == 4250.0
    triggers = {t["trigger"]: t for t in summary["per_trigger"]}
    assert triggers["CONFIDENCE_THRESHOLD"]["net_pnl"] == 2300.0
    assert triggers["BOS_STRUCTURE"]["net_pnl"] == 1650.0
    assert triggers["MANUAL"]["net_pnl"] == 300.0
