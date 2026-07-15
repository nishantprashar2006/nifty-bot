"""
Regression tests for the v1.13 pre-flight margin check (Fix A) and broker
rejection surfacing (Fix B) added around `_place_entry` / `_handle_manual_entry`.

Locks in:
  • Enough balance → order submitted normally (no preflight false-block).
  • Insufficient balance → no broker call, no ORDER_PENDING, structured
    PRECHECK_FAILED context returned to the API.
  • Preflight passes, broker rejects → ORDER_PENDING never entered,
    structured BROKER_REJECTED context with the full broker message
    returned to the API.
  • RMS lookup failure never blocks trading (broker remains authority).
  • Timeline events PRECHECK_FAILED / ORDER_REJECTED emitted exactly once.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("BOT_DB_PATH", tempfile.mktemp(suffix=".db"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                                          # noqa: E402
from broker.smartapi_client import SmartApiError       # noqa: E402
from data.option_selector import OptionContract        # noqa: E402
from database.sqlite_logger import SqliteLogger        # noqa: E402
from strategy.position_manager import PositionManager  # noqa: E402


def _make_bot(available_cash: float | Exception = 1_000_000.0,
              place_result=None):
    """Bare-bones bot instance wired for `_place_entry` / `_handle_manual_entry`.

    `available_cash` — value (or Exception subclass to raise) for
                       `broker.get_net_available_cash()`.
    `place_result`   — return value or Exception for `broker.place_order()`.
    """
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._pending_source = "manual"
    bot._pending_engine = "smc"
    bot._pending_confidence = 72
    bot._pending_reasons = ["test"]
    bot.state = config.State.IDLE
    bot._state_lock = threading.RLock()
    bot._api_reject_count = 0
    bot._trades_today = 0
    bot._consecutive_losses = 0
    bot._last_option_quote: dict = {}
    bot._last_reject_context = None
    bot._effective_lots = 1
    bot._exit_reason_hint = None
    bot._timeline_session = "S-tests"
    bot.timeline = MagicMock()

    # Prime a fresh WS-style tick for the contract token used by _make_contract.
    bot._last_option_quote["55555"] = {"ltp": 100.0, "bid": 99.5, "ask": 100.5,
                                       "volume": 1000, "oi": 1000, "ts": 0.0}

    transitions: list = []

    def _fake_transition(new_state):
        transitions.append(new_state)
        bot.state = new_state
    bot._transition = _fake_transition

    # Broker mock
    bot.broker = MagicMock()
    if isinstance(available_cash, Exception) or (
        isinstance(available_cash, type) and issubclass(available_cash, Exception)
    ):
        bot.broker.get_net_available_cash.side_effect = available_cash
    else:
        bot.broker.get_net_available_cash.return_value = float(available_cash)
    if isinstance(place_result, Exception):
        bot.broker.place_order.side_effect = place_result
    elif place_result is not None:
        bot.broker.place_order.return_value = place_result

    # Sizer stub — returns 1 lot regardless.
    sizer = MagicMock()
    sizer.premium_spike_guard.return_value = 1
    bot.sizer = sizer

    # WS stub — one recent tick per token; ATM refresh is not exercised
    # because we call `_place_entry` directly with a prepared contract.
    ws = MagicMock()
    ws.get_last_tick.return_value = MagicMock(ltp=100.0, ts=0.0)
    ws.reconnect_failures = 0
    bot.ws = ws
    bot.pnl_guard = None

    return bot, transitions


def _make_contract():
    return OptionContract(
        symbol="NIFTY24500CE", token="55555", exchange="NFO",
        strike=24500.0, expiry="07FEB26", option_type="CE",
        lot_size=config.LOT_SIZE_NIFTY,
    )


@pytest.fixture(autouse=True)
def _force_live_preflight():
    """v2.0.1 — the pre-flight is now LIVE-only. Force LIVE mode for all
    v1.13 tests so the funds check actually runs."""
    original = config.SIMULATE_ORDERS
    config.SIMULATE_ORDERS = False
    yield
    config.SIMULATE_ORDERS = original


# ─────────────────────────────────────────────── Test 1 — Enough balance
def test_enough_balance_order_submitted():
    bot, transitions = _make_bot(available_cash=1_000_000.0, place_result="ORD-A")
    ok = bot._place_entry(
        config.Direction.LONG, _make_contract(),
        sl_pts=0.0, tp_pts=0.0, source="manual",
        sl_pct=config.MANUAL_SL_PCT, tp_pct=config.MANUAL_TP_PCT,
        trail_step_pct=config.TRAIL_STEP_PCT,
    )
    assert ok is True
    bot.broker.place_order.assert_called_once()
    assert bot._last_reject_context is None
    assert config.State.ORDER_PENDING in transitions


# ─────────────────────────────────────────────── Test 2 — Insufficient balance
def test_insufficient_balance_blocks_broker_call():
    # qty = 1 * 65 = 65, premium = 100.0, required ≈ 6825 * 1.05 = 7166
    # so set available way below.
    bot, transitions = _make_bot(available_cash=100.0, place_result="never")
    ok = bot._place_entry(
        config.Direction.LONG, _make_contract(),
        sl_pts=0.0, tp_pts=0.0, source="manual",
        sl_pct=config.MANUAL_SL_PCT, tp_pct=config.MANUAL_TP_PCT,
        trail_step_pct=config.TRAIL_STEP_PCT,
    )
    assert ok is False
    # NO broker submission
    bot.broker.place_order.assert_not_called()
    # NO ORDER_PENDING transition
    assert config.State.ORDER_PENDING not in transitions
    # Structured context populated
    ctx = bot._last_reject_context
    assert ctx is not None
    assert ctx["phase"] == "preflight"
    assert ctx["broker_status"] == "not_submitted"
    assert ctx["broker_reason"] == "insufficient_funds"
    assert ctx["available"] == pytest.approx(100.0)
    assert ctx["required"] > ctx["available"]
    # Timeline event emitted exactly once with the required fields
    from execution_timeline import Event
    calls = [c for c in bot.timeline.log.call_args_list
             if len(c.args) >= 2 and c.args[1] == Event.PRECHECK_FAILED]
    assert len(calls) == 1
    payload = calls[0].args[3]
    assert "available" in payload and "required" in payload


# ─────────────────────────────────────────────── Test 3 — Preflight ok, broker rejects
def test_preflight_pass_broker_rejects():
    bot, transitions = _make_bot(
        available_cash=1_000_000.0,
        place_result=SmartApiError("RMS: Margin Exceeds — Available ₹100, Required ₹7000"),
    )
    ok = bot._place_entry(
        config.Direction.LONG, _make_contract(),
        sl_pts=0.0, tp_pts=0.0, source="manual",
        sl_pct=config.MANUAL_SL_PCT, tp_pct=config.MANUAL_TP_PCT,
        trail_step_pct=config.TRAIL_STEP_PCT,
    )
    assert ok is False
    # Broker WAS called (preflight passed).
    bot.broker.place_order.assert_called_once()
    # NO ORDER_PENDING transition (SmartApiError happened before).
    assert config.State.ORDER_PENDING not in transitions
    # Structured context captures the full broker message verbatim.
    ctx = bot._last_reject_context
    assert ctx["phase"] == "broker"
    assert ctx["broker_status"] == "rejected"
    assert "RMS: Margin Exceeds" in ctx["broker_reason"]
    # Timeline event emitted.
    from execution_timeline import Event
    calls = [c for c in bot.timeline.log.call_args_list
             if len(c.args) >= 2 and c.args[1] == Event.ORDER_REJECTED]
    assert len(calls) == 1
    payload = calls[0].args[3]
    assert payload["reason"].startswith("RMS: Margin Exceeds")


# ─────────────────────────────────────────────── Test 4 — RMS-style rejection msg
def test_broker_rms_rejection_message_preserved_verbatim():
    reason = "AB1004: Insufficient Funds. Available ₹41,250 Required ₹56,400"
    bot, transitions = _make_bot(
        available_cash=1_000_000.0, place_result=SmartApiError(reason),
    )
    bot._place_entry(
        config.Direction.LONG, _make_contract(),
        sl_pts=0.0, tp_pts=0.0, source="manual",
        sl_pct=config.MANUAL_SL_PCT, tp_pct=config.MANUAL_TP_PCT,
        trail_step_pct=config.TRAIL_STEP_PCT,
    )
    assert reason in bot._last_reject_context["broker_reason"]
    assert reason in bot._last_reject_context["user_message"]


# ─────────────────────────────────────────────── Test 5 — Exchange rejection msg
def test_broker_exchange_rejection_message_preserved_verbatim():
    reason = "Exchange rejected: Invalid symbol / Market closed"
    bot, transitions = _make_bot(
        available_cash=1_000_000.0, place_result=SmartApiError(reason),
    )
    bot._place_entry(
        config.Direction.LONG, _make_contract(),
        sl_pts=0.0, tp_pts=0.0, source="manual",
        sl_pct=config.MANUAL_SL_PCT, tp_pct=config.MANUAL_TP_PCT,
        trail_step_pct=config.TRAIL_STEP_PCT,
    )
    assert reason in bot._last_reject_context["broker_reason"]


# ─────────────────────────────────────────────── Test 6 — RMS lookup fails
def test_rms_lookup_failure_does_not_block_trading():
    # `sizer.premium_spike_guard(current_equity=...)` also calls
    # get_net_available_cash(). Configure the mock to succeed for the sizer
    # (first call) and fail for the preflight (second call) — this exactly
    # models a transient RMS glitch in production.
    bot, transitions = _make_bot(available_cash=1_000_000.0, place_result="ORD-Z")
    bot.broker.get_net_available_cash.side_effect = [1_000_000.0, RuntimeError("network down")]
    ok = bot._place_entry(
        config.Direction.LONG, _make_contract(),
        sl_pts=0.0, tp_pts=0.0, source="manual",
        sl_pct=config.MANUAL_SL_PCT, tp_pct=config.MANUAL_TP_PCT,
        trail_step_pct=config.TRAIL_STEP_PCT,
    )
    # Preflight silently deferred to broker.
    assert ok is True
    bot.broker.place_order.assert_called_once()
    assert bot._last_reject_context is None
    assert config.State.ORDER_PENDING in transitions


# ─────────────────────────────────────────────── Test 7 — Timeline verification
def test_precheck_and_reject_events_emitted_exactly_once():
    from execution_timeline import Event

    # (a) Preflight failure → PRECHECK_FAILED once, ORDER_REJECTED never.
    bot, _ = _make_bot(available_cash=100.0)
    bot._place_entry(
        config.Direction.LONG, _make_contract(),
        sl_pts=0.0, tp_pts=0.0, source="manual",
        sl_pct=config.MANUAL_SL_PCT, tp_pct=config.MANUAL_TP_PCT,
        trail_step_pct=config.TRAIL_STEP_PCT,
    )
    precheck = [c for c in bot.timeline.log.call_args_list
                if len(c.args) >= 2 and c.args[1] == Event.PRECHECK_FAILED]
    reject = [c for c in bot.timeline.log.call_args_list
              if len(c.args) >= 2 and c.args[1] == Event.ORDER_REJECTED]
    assert len(precheck) == 1
    assert len(reject) == 0

    # (b) Broker rejection → PRECHECK_FAILED never, ORDER_REJECTED once.
    bot, _ = _make_bot(available_cash=1_000_000.0,
                       place_result=SmartApiError("boom"))
    bot._place_entry(
        config.Direction.LONG, _make_contract(),
        sl_pts=0.0, tp_pts=0.0, source="manual",
        sl_pct=config.MANUAL_SL_PCT, tp_pct=config.MANUAL_TP_PCT,
        trail_step_pct=config.TRAIL_STEP_PCT,
    )
    precheck = [c for c in bot.timeline.log.call_args_list
                if len(c.args) >= 2 and c.args[1] == Event.PRECHECK_FAILED]
    reject = [c for c in bot.timeline.log.call_args_list
              if len(c.args) >= 2 and c.args[1] == Event.ORDER_REJECTED]
    assert len(precheck) == 0
    assert len(reject) == 1


# ─────────────────────────────────────────────── extra — API surface
def test_list_commands_parses_structured_result():
    """The server-side `list_commands` endpoint decodes the structured
    result JSON so the UI can render broker_status / broker_reason /
    user_message without having to parse strings itself."""
    from server import list_commands, _conn
    from datetime import datetime, timezone
    ctx = {
        "phase": "broker",
        "broker_status": "rejected",
        "broker_reason": "RMS: Margin Exceeds",
        "user_message": "Broker rejected the order — RMS: Margin Exceeds",
        "symbol": "NIFTY24500CE",
        "qty": 65,
        "ref_price": 100.0,
    }
    with _conn() as c:
        c.execute(
            "INSERT INTO commands(timestamp, action, payload, status, result) "
            "VALUES (?, 'manual_entry', ?, 'fail', ?)",
            (datetime.now(timezone.utc).isoformat(), "{}",
             "BROKER_REJECTED: " + json.dumps(ctx, separators=(",", ":"))),
        )
    rows = list_commands(limit=5)
    # find the most recent manual_entry that has our marker
    ours = None
    for r in rows:
        if r.get("action") == "manual_entry" and r.get("broker_status") == "rejected":
            ours = r
            break
    assert ours is not None, f"could not find inserted row in {rows[:2]}"
    assert ours["broker_reason"] == "RMS: Margin Exceeds"
    assert "Broker rejected the order" in ours["user_message"]
    assert ours["rejection"]["tag"] == "BROKER_REJECTED"


def test_manual_entry_returns_structured_precheck_reason():
    """`_handle_manual_entry` must package the pre-flight rejection into
    a machine-parseable PRECHECK_FAILED: ... JSON string so the
    commands table row carries structured data."""
    bot, _ = _make_bot(available_cash=100.0)
    # Bypass ATM refresh by pre-populating _ce and disabling the refresh.
    bot._ce = _make_contract()
    bot._pe = _make_contract()
    bot._refresh_atm_contracts = MagicMock()
    ok, msg = bot._handle_manual_entry(config.Direction.LONG, lots_override=1)
    assert ok is False
    assert msg.startswith("PRECHECK_FAILED: ")
    payload = json.loads(msg[len("PRECHECK_FAILED: "):])
    assert payload["broker_reason"] == "insufficient_funds"
    assert payload["available"] < payload["required"]
