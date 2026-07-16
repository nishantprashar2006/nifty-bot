"""
v2.2 P0 — regression tests for the broker_capital snapshot exposed by
GET /api/bot/status and the periodic publisher on the FSM main loop.

The UI must show ONE authoritative capital number (SIM=sim_capital,
LIVE=broker RMS) so the operator never sees the pre-v2.2 drift where
"Live Cash" and "Broker Capital" disagreed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("BOT_DB_PATH", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                                              # noqa: E402
from database.sqlite_logger import SqliteLogger            # noqa: E402


# ─── server._broker_capital_snapshot ──────────────────────────────────
def _fresh_server_db(tmp_path):
    db_path = str(tmp_path / "server_snap.db")
    os.environ["BOT_DB_PATH"] = db_path
    # server.py reads DB_PATH from env at import time; reload if needed
    import importlib
    import server as _srv
    importlib.reload(_srv)
    return _srv, db_path


def test_snapshot_sim_reads_sim_capital(tmp_path, monkeypatch):
    srv, db_path = _fresh_server_db(tmp_path)
    monkeypatch.setattr(srv, "_current_trading_mode", lambda: "sim")
    monkeypatch.setattr(srv, "_read_bot_state", lambda k, d="": "175000" if k == "sim_capital" else d)
    snap = srv._broker_capital_snapshot()
    assert snap["source"] == "sim"
    assert snap["trading_mode"] == "sim"
    assert snap["value"] == 175000.0


def test_snapshot_live_reads_bot_state_broker_capital(tmp_path, monkeypatch):
    srv, db_path = _fresh_server_db(tmp_path)
    monkeypatch.setattr(srv, "_current_trading_mode", lambda: "live")
    payload = json.dumps({"value": 42315.5, "source": "broker", "ts": 1234.5})
    monkeypatch.setattr(srv, "_read_bot_state", lambda k, d="": payload if k == "broker_capital" else d)
    snap = srv._broker_capital_snapshot()
    assert snap["source"] == "broker"
    assert snap["trading_mode"] == "live"
    assert snap["value"] == 42315.5


def test_snapshot_live_fallback_when_missing(tmp_path, monkeypatch):
    """LIVE mode with no bot_state.broker_capital and no equity_curve
    row must return zero (never crash, never fabricate)."""
    srv, db_path = _fresh_server_db(tmp_path)
    monkeypatch.setattr(srv, "_current_trading_mode", lambda: "live")
    monkeypatch.setattr(srv, "_read_bot_state", lambda k, d="": d)
    snap = srv._broker_capital_snapshot()
    assert snap["trading_mode"] == "live"
    assert snap["value"] == 0.0


# ─── main._publish_broker_capital (bot side) ──────────────────────────
def _make_bot():
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot.broker = MagicMock()
    bot.state = config.State.IDLE
    bot._state_lock = threading.RLock()
    return bot


def test_publish_broker_capital_sim_writes_sim_capital():
    bot = _make_bot()
    bot.db.set_state("sim_capital", "150000")
    original_sim = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = True
        bot._publish_broker_capital()
        raw = bot.db.get_state("broker_capital")
        assert raw, "broker_capital must be written to bot_state"
        payload = json.loads(raw[0])
        assert payload["source"] == "sim"
        assert payload["value"] == 150000.0
        # SIM path must NOT touch broker
        bot.broker.get_net_available_cash.assert_not_called()
    finally:
        config.SIMULATE_ORDERS = original_sim


def test_publish_broker_capital_live_fetches_broker():
    bot = _make_bot()
    bot.broker.get_net_available_cash.return_value = 87500.25
    original_sim = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = False
        bot._publish_broker_capital()
        raw = bot.db.get_state("broker_capital")
        assert raw
        payload = json.loads(raw[0])
        assert payload["source"] == "broker"
        assert payload["value"] == 87500.25
        bot.broker.get_net_available_cash.assert_called_once()
    finally:
        config.SIMULATE_ORDERS = original_sim


def test_publish_broker_capital_throttled_30s():
    """Two back-to-back calls within 30s must issue at most one broker RPC."""
    bot = _make_bot()
    bot.broker.get_net_available_cash.return_value = 55000.0
    original_sim = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = False
        bot._publish_broker_capital()
        bot._publish_broker_capital()
        bot._publish_broker_capital()
        # First call goes through, subsequent calls are within the 30s
        # throttle window and must be skipped.
        assert bot.broker.get_net_available_cash.call_count == 1
    finally:
        config.SIMULATE_ORDERS = original_sim


def test_publish_broker_capital_broker_failure_is_swallowed():
    """RMS lookup failure must never crash the main loop."""
    bot = _make_bot()
    bot.broker.get_net_available_cash.side_effect = RuntimeError("network")
    original_sim = config.SIMULATE_ORDERS
    try:
        config.SIMULATE_ORDERS = False
        # Should not raise
        bot._publish_broker_capital()
    finally:
        config.SIMULATE_ORDERS = original_sim


# ─── sim_capital persistence end-to-end ───────────────────────────────
def test_sizing_config_post_persists_sim_capital(tmp_path, monkeypatch):
    """POST /api/bot/sizing_config with sim_capital must survive a
    hypothetical restart (i.e. subsequent GET returns the new value)."""
    srv, db_path = _fresh_server_db(tmp_path)
    from fastapi.testclient import TestClient
    client = TestClient(srv.app)

    r = client.post("/api/bot/sizing_config", json={"sim_capital": 123456})
    assert r.status_code == 200
    assert r.json()["sim_capital"] == 123456.0

    # Simulate a fresh read (as a restart would)
    r2 = client.get("/api/bot/sizing_config")
    assert r2.status_code == 200
    assert r2.json()["sim_capital"] == 123456.0
