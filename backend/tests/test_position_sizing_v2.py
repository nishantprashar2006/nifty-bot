"""
Regression tests for v2.0 Auto Risk-Based Position Sizing +
the lots-persistence bug fix (GET /bot/manual_lots now honours the
operator's saved value).

Locks in:
  • `_compute_auto_risk_lots` deterministic math with floor().
  • max_lots and minimum-1-lot clamping.
  • SIM path uses `sim_capital`; LIVE path fetches from broker; broker
    failure returns `error` and prevents the trade.
  • Config validation rejects out-of-range values.
  • Persistence across reads (DB round-trip).
  • Lots-persistence bug fix: saved default_lots > dynamic auto-calc.
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

import config                                            # noqa: E402
from database.sqlite_logger import SqliteLogger          # noqa: E402
from strategy.position_manager import PositionManager    # noqa: E402


def _make_bot():
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot.state = config.State.IDLE
    bot._state_lock = threading.RLock()
    bot._auto_suspended_reason = None
    bot._exit_reason_hint = None
    bot._timeline_session = None
    bot.timeline = MagicMock()
    bot.broker = MagicMock()
    bot.broker.get_net_available_cash.return_value = 500000.0
    return bot


# ────────── formula ──────────
def test_auto_sizing_correct_formula_sim():
    bot = _make_bot()
    bot.db.set_state("risk_pct", "1.0")
    bot.db.set_state("max_lots", "10")
    bot.db.set_state("sim_capital", "200000")
    # Force SIM path (broker not called)
    r = bot._compute_auto_risk_lots(entry_ref_price=100.0)
    # risk_amount = 200000 * 0.01 = 2000
    # loss_per_lot = 100 * 0.15 * 65 = 975  (uses MANUAL_SL_PCT=0.15)
    # calculated = floor(2000/975) = 2
    assert r["capital"] == 200000
    assert r["risk_amount"] == pytest.approx(2000.0)
    assert r["loss_per_lot"] == pytest.approx(100 * config.MANUAL_SL_PCT * config.LOT_SIZE_NIFTY, rel=1e-3)
    assert r["calculated_lots"] == 2
    assert r["final_lots"] == 2
    assert "error" not in r
    # SIM must not call the broker.
    bot.broker.get_net_available_cash.assert_not_called()


def test_auto_sizing_respects_max_lots():
    bot = _make_bot()
    bot.db.set_state("risk_pct", "10.0")   # huge risk
    bot.db.set_state("max_lots", "3")      # tight cap
    bot.db.set_state("sim_capital", "10000000")  # huge capital
    r = bot._compute_auto_risk_lots(entry_ref_price=50.0)
    assert r["final_lots"] == 3
    assert r["calculated_lots"] > r["final_lots"]


def test_auto_sizing_minimum_one_lot():
    bot = _make_bot()
    bot.db.set_state("risk_pct", "0.1")
    bot.db.set_state("max_lots", "5")
    bot.db.set_state("sim_capital", "5000")   # tiny cap
    r = bot._compute_auto_risk_lots(entry_ref_price=200.0)
    # floor might be 0, but final_lots must be at least 1.
    assert r["final_lots"] >= 1
    # Note: user's spec says minimum 1 lot even if math gives 0.


def test_auto_sizing_rejects_invalid_risk_pct():
    bot = _make_bot()
    bot.db.set_state("risk_pct", "0")
    bot.db.set_state("sim_capital", "200000")
    r = bot._compute_auto_risk_lots(100.0)
    assert "error" in r
    assert "risk_pct" in r["error"]
    assert r["final_lots"] == 0


def test_auto_sizing_rejects_invalid_max_lots():
    bot = _make_bot()
    bot.db.set_state("risk_pct", "1.0")
    bot.db.set_state("max_lots", "0")
    r = bot._compute_auto_risk_lots(100.0)
    assert "error" in r
    assert "max_lots" in r["error"]


def test_auto_sizing_rejects_missing_entry_price():
    bot = _make_bot()
    bot.db.set_state("risk_pct", "1.0")
    r = bot._compute_auto_risk_lots(0.0)
    assert "error" in r
    assert "entry" in r["error"].lower()


def test_auto_sizing_live_broker_failure_returns_error():
    """When LIVE broker fetch fails, the sizer MUST refuse (no trade)."""
    bot = _make_bot()
    bot.broker.get_net_available_cash.side_effect = RuntimeError("timeout")
    # Force USE_LIVE_BROKER flag temporarily.
    original_live = config.USE_LIVE_BROKER
    original_mode = getattr(config, "TRADING_MODE", "sim")
    try:
        config.USE_LIVE_BROKER = True
        config.TRADING_MODE = "live"
        r = bot._compute_auto_risk_lots(100.0)
        assert "error" in r
        assert "Unable to fetch available funds" in r["error"]
        assert r["final_lots"] == 0
    finally:
        config.USE_LIVE_BROKER = original_live
        config.TRADING_MODE = original_mode


def test_auto_sizing_deterministic_across_repeat_calls():
    bot = _make_bot()
    bot.db.set_state("risk_pct", "1.5")
    bot.db.set_state("max_lots", "8")
    bot.db.set_state("sim_capital", "350000")
    r1 = bot._compute_auto_risk_lots(120.5)
    r2 = bot._compute_auto_risk_lots(120.5)
    assert r1["final_lots"] == r2["final_lots"]
    assert r1["calculated_lots"] == r2["calculated_lots"]


# ────────── API endpoint validation ──────────
def test_sizing_config_endpoint_rejects_invalid_values():
    from server import set_sizing_config, SizingConfigRequest
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        set_sizing_config(SizingConfigRequest(risk_pct=15.0))
    with pytest.raises(HTTPException):
        set_sizing_config(SizingConfigRequest(risk_pct=0.0))
    with pytest.raises(HTTPException):
        set_sizing_config(SizingConfigRequest(max_lots=0))
    with pytest.raises(HTTPException):
        set_sizing_config(SizingConfigRequest(sim_capital=0))
    with pytest.raises(HTTPException):
        set_sizing_config(SizingConfigRequest(sizing_mode="bogus"))


def test_sizing_config_endpoint_persists_all_fields():
    from server import (set_sizing_config, get_sizing_config,
                        SizingConfigRequest, _read_bot_state)
    r = set_sizing_config(SizingConfigRequest(
        sizing_mode="auto_risk", risk_pct=2.5, max_lots=7,
        sim_capital=500000, default_lots=3,
    ))
    assert r["sizing_mode"] == "auto_risk"
    assert r["risk_pct"] == 2.5
    assert r["max_lots"] == 7
    assert r["sim_capital"] == 500000
    assert r["default_lots"] == 3
    # Round-trip via GET
    g = get_sizing_config()
    assert g["sizing_mode"] == "auto_risk"
    assert g["risk_pct"] == 2.5
    # DB rows exist for cross-process reads.
    assert _read_bot_state("sizing_mode") == "auto_risk"
    assert _read_bot_state("risk_pct") == "2.5"


# ────────── Lots persistence bug ──────────
def test_manual_lots_endpoint_prefers_saved_value():
    """Bug fix: `/bot/manual_lots` used to always return the dynamically
    calculated value, ignoring the operator's saved lot count. It must
    now prefer `bot_state.default_lots` when the operator has saved one."""
    from server import manual_lots_default, _write_bot_state
    _write_bot_state("default_lots", "7")
    resp = manual_lots_default()
    assert resp["default_lots"] == 7
    assert resp.get("source") == "user_saved"


def test_manual_lots_endpoint_falls_back_to_auto_when_no_saved():
    """When no saved preference exists, fall back to the legacy
    drawdown-aware auto calc — preserves backward-compat for fresh
    installs."""
    from server import manual_lots_default, _write_bot_state
    # Clear any prior saved value.
    _write_bot_state("default_lots", "")
    resp = manual_lots_default()
    # Auto calc must still return a positive integer.
    assert resp["default_lots"] >= 1
    assert resp.get("source") == "auto"
