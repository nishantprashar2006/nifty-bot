"""
Regression tests for the v1.12 ORDER_PENDING timeout reconciliation
(Fix A + Fix B) added to `_step_order_pending()`.

The bug this file locks down:
    Broker executed a manual BUY order but the SmartWebSocket
    on_order(status="complete") event was not delivered inside
    ORDER_TIMEOUT_SEC. Before the fix, the FSM cancelled the in-memory
    pending and went IDLE, leaving an unprotected live position on the
    broker. After the fix, `_step_order_pending` polls the broker at the
    timeout boundary and routes any confirmed fill through the existing
    `_handle_fill()` pipeline — reusing all downstream logic (promote,
    protective legs, DB insert, POSITION_OPEN transition, timeline).

Every test builds a stripped-down `NiftyOptionsBot` via `__new__` and
patches only what the reconciliation function touches: `broker`,
`positions`, `db`, `_place_protective_legs`, `_transition`, `timeline`.
Nothing in the rest of the FSM is exercised.
"""
from __future__ import annotations

import os
import tempfile
import threading
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("BOT_DB_PATH", tempfile.mktemp(suffix=".db"))

import config                                                # noqa: E402
from config import Direction                                 # noqa: E402
from database.sqlite_logger import SqliteLogger              # noqa: E402
from strategy.position_manager import PendingEntry, PositionManager  # noqa: E402


# ─────────────────────────────────────────────── helpers
def _make_bot(order_id: str = "ORD-1"):
    """Bare-bones bot instance wired for `_step_order_pending`.

    Returns (bot, transitions_list). `transitions_list` records every
    state passed to `_transition` so tests can assert POSITION_OPEN
    vs IDLE outcomes without booting the real FSM."""
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._pending_source = "manual"
    bot._pending_engine = "smc"
    bot._pending_confidence = 72
    bot._pending_reasons = ["test"]
    bot.state = config.State.ORDER_PENDING
    bot._state_lock = threading.RLock()
    bot._place_protective_legs = MagicMock()
    bot.broker = MagicMock()
    bot.timeline = MagicMock()
    bot._timeline_session = None
    bot._exit_reason_hint = None

    transitions: list = []

    def _fake_transition(new_state):
        transitions.append(new_state)
        bot.state = new_state
    bot._transition = _fake_transition

    bot.positions.register_pending_entry(PendingEntry(
        order_id=order_id, direction=Direction.LONG,
        contract_symbol="NIFTY24500CE", contract_token="55555",
        expected_price=100.0, lots=1, qty=65,
        target_price=130.0, stop_price=85.0,
        strike=24500.0, expiry="07FEB26", option_type="CE", lot_size=65,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    ))
    return bot, transitions


def _force_pending_expired(bot):
    """Age the pending order beyond ORDER_TIMEOUT_SEC by rewinding placed_ts."""
    import time as _t
    bot.positions.pending_entry.placed_ts = _t.time() - config.ORDER_TIMEOUT_SEC - 1


# ─────────────────────────────────────────────── Test 1
def test_normal_ws_fill_triggers_no_reconciliation():
    """When the WS fill already promoted the pending BEFORE the timeout
    branch runs, `_step_order_pending` must be a no-op. `broker.order_book`
    and `broker.positions` must NEVER be called."""
    bot, transitions = _make_bot()
    # Simulate WS fill already fired: pending is None, open_position is set.
    from broker.websocket_manager import OrderEvent
    ev = OrderEvent(order_id="ORD-1", status="complete",
                    fill_price=0.0, avg_price=100.0, text="ws", ts=0.0)
    bot._handle_fill(ev)  # promotes -> POSITION_OPEN
    assert bot.positions.pending_entry is None
    # Reset broker mocks to detect any spurious calls.
    bot.broker.order_book.reset_mock()
    bot.broker.positions.reset_mock()

    # Now call _step_order_pending — nothing to do.
    bot._step_order_pending()
    bot.broker.order_book.assert_not_called()
    bot.broker.positions.assert_not_called()


# ─────────────────────────────────────────────── Test 2
def test_orderbook_reconciliation_promotes_to_open_position():
    """WS fill missed. order_book() reports COMPLETE with averageprice.
    Reconciliation must route through `_handle_fill` → promote, place
    protection, transition to POSITION_OPEN."""
    bot, transitions = _make_bot(order_id="ORD-2")
    _force_pending_expired(bot)

    bot.broker.order_book.return_value = [
        {"orderid": "OTHER", "status": "open"},
        {"orderid": "ORD-2", "status": "complete", "averageprice": 102.5},
    ]

    bot._step_order_pending()

    assert bot.broker.order_book.call_count == 1
    # positions() must NOT be queried when order_book already confirmed.
    bot.broker.positions.assert_not_called()
    pos = bot.positions.open_position
    assert pos is not None, "expected promotion to open position"
    assert pos.entry_price == pytest.approx(102.5)
    # Protection was placed exactly once.
    assert bot._place_protective_legs.call_count == 1
    # FSM transitioned to POSITION_OPEN (never IDLE).
    assert config.State.POSITION_OPEN in transitions
    assert config.State.IDLE not in transitions
    # cancel_order must NOT have been called — order was COMPLETE.
    bot.broker.cancel_order.assert_not_called()


# ─────────────────────────────────────────────── Test 3
def test_positions_reconciliation_when_orderbook_lacks_completion():
    """order_book() shows no matching row OR only non-complete statuses.
    positions() shows an open NFO position matching our token/direction.
    Reconciliation must route through `_handle_fill` and adopt."""
    bot, transitions = _make_bot(order_id="ORD-3")
    _force_pending_expired(bot)

    # order_book has our order but as 'open', not complete → Fix A skips.
    bot.broker.order_book.return_value = [
        {"orderid": "ORD-3", "status": "open", "averageprice": 0.0},
    ]
    # positions() confirms open long NFO position.
    bot.broker.positions.return_value = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY24500CE",
         "symboltoken": "55555", "netqty": "65", "avgnetprice": 101.75},
    ]

    bot._step_order_pending()

    pos = bot.positions.open_position
    assert pos is not None
    assert pos.entry_price == pytest.approx(101.75)
    assert bot._place_protective_legs.call_count == 1
    assert config.State.POSITION_OPEN in transitions
    assert config.State.IDLE not in transitions
    bot.broker.cancel_order.assert_not_called()


# ─────────────────────────────────────────────── Test 4
def test_no_reconciliation_falls_back_to_cancel_and_idle():
    """Neither source proves a fill → preserve legacy cancel + IDLE."""
    bot, transitions = _make_bot(order_id="ORD-4")
    _force_pending_expired(bot)

    bot.broker.order_book.return_value = [
        {"orderid": "ORD-4", "status": "open", "averageprice": 0.0},
    ]
    bot.broker.positions.return_value = []   # broker is flat

    bot._step_order_pending()

    # No promotion.
    assert bot.positions.open_position is None
    # Legacy cancel + IDLE preserved.
    bot.broker.cancel_order.assert_called_once_with("ORD-4")
    assert config.State.IDLE in transitions
    assert config.State.POSITION_OPEN not in transitions
    # Protection must not have been placed.
    bot._place_protective_legs.assert_not_called()


# ─────────────────────────────────────────────── Test 5
def test_duplicate_fill_prevention_ws_first_then_reconciliation():
    """WebSocket fill arrives, then the timeout branch runs. The
    reconciliation must be a no-op — no duplicate trade row, no duplicate
    protection, no duplicate promotion."""
    bot, transitions = _make_bot(order_id="ORD-5")
    _force_pending_expired(bot)

    # WebSocket fill fires first.
    from broker.websocket_manager import OrderEvent
    ev = OrderEvent(order_id="ORD-5", status="complete",
                    fill_price=0.0, avg_price=99.0, text="ws-first", ts=0.0)
    bot._handle_fill(ev)
    assert bot.positions.open_position is not None
    assert bot.positions.pending_entry is None
    protect_calls_after_ws = bot._place_protective_legs.call_count

    # Set up order_book to *also* claim COMPLETE — reconciliation must
    # detect the empty pending and skip.
    bot.broker.order_book.return_value = [
        {"orderid": "ORD-5", "status": "complete", "averageprice": 99.0},
    ]

    bot._step_order_pending()

    # order_book was NOT queried because pending is None short-circuits early.
    bot.broker.order_book.assert_not_called()
    # No additional protection calls.
    assert bot._place_protective_legs.call_count == protect_calls_after_ws
    # Still exactly one open position, exactly one entry price.
    assert bot.positions.open_position.entry_price == pytest.approx(99.0)


# ─────────────────────────────────────────────── Test 6
def test_reconciliation_after_broker_open_is_idempotent():
    """If reconciliation runs twice back-to-back (e.g. two timeout ticks
    interleaved with a slow response), the second call must be a no-op."""
    bot, transitions = _make_bot(order_id="ORD-6")
    _force_pending_expired(bot)

    bot.broker.order_book.return_value = [
        {"orderid": "ORD-6", "status": "complete", "averageprice": 100.0},
    ]

    bot._step_order_pending()
    first_protect_count = bot._place_protective_legs.call_count
    first_transitions = list(transitions)

    # Second call: state is POSITION_OPEN, pending is None. The method's
    # early guard (pending_entry is None) short-circuits before touching the
    # broker or protection — that is the real idempotency guarantee.
    bot.broker.order_book.reset_mock()
    bot.broker.positions.reset_mock()
    bot._step_order_pending()

    # No second promotion, no second protection.
    assert bot._place_protective_legs.call_count == first_protect_count
    # No broker reconciliation queries on the second call.
    bot.broker.order_book.assert_not_called()
    bot.broker.positions.assert_not_called()
    # The still-open position is untouched (same entry price, same object).
    pos = bot.positions.open_position
    assert pos is not None
    assert pos.entry_price == pytest.approx(100.0)


# ─────────────────────────────────────────────── Test 7
def test_timeline_events_emitted_exactly_once_on_recovery():
    """Recovery via order_book() emits ORDER_PENDING_RECONCILE_ORDERBOOK
    exactly once with the required fields."""
    from execution_timeline import Event
    bot, transitions = _make_bot(order_id="ORD-7")
    _force_pending_expired(bot)

    # Prime a timeline session id so `_tl(session, ...)` fires.
    bot._timeline_session = "S-test-session"

    bot.broker.order_book.return_value = [
        {"orderid": "ORD-7", "status": "complete", "averageprice": 104.0},
    ]

    bot._step_order_pending()

    # Collect timeline.log calls (called via self._tl helper).
    tl_calls = bot.timeline.log.call_args_list
    matched = [
        c for c in tl_calls
        if len(c.args) >= 2 and c.args[1] == Event.ORDER_PENDING_RECONCILE_ORDERBOOK
    ]
    assert len(matched) == 1, f"expected exactly 1 reconcile timeline event, got {len(matched)}"
    payload = matched[0].args[3] if len(matched[0].args) >= 4 else matched[0].kwargs.get("payload")
    assert payload is not None
    assert payload["order_id"] == "ORD-7"
    assert payload["broker_status"] == "complete"
    assert payload["avg_price"] == pytest.approx(104.0)
    assert payload["trigger_reason"] == "order_pending_timeout"


def test_timeline_events_emitted_exactly_once_on_positions_recovery():
    """Recovery via positions() emits ORDER_PENDING_RECONCILE_POSITION."""
    from execution_timeline import Event
    bot, transitions = _make_bot(order_id="ORD-8")
    _force_pending_expired(bot)

    bot._timeline_session = "S-test-session-2"

    bot.broker.order_book.return_value = [
        {"orderid": "ORD-8", "status": "open", "averageprice": 0.0},
    ]
    bot.broker.positions.return_value = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY24500CE",
         "symboltoken": "55555", "netqty": "65", "avgnetprice": 103.5},
    ]

    bot._step_order_pending()

    tl_calls = bot.timeline.log.call_args_list
    matched = [
        c for c in tl_calls
        if len(c.args) >= 2 and c.args[1] == Event.ORDER_PENDING_RECONCILE_POSITION
    ]
    assert len(matched) == 1
    payload = matched[0].args[3] if len(matched[0].args) >= 4 else matched[0].kwargs.get("payload")
    assert payload["symbol"] == "NIFTY24500CE"
    assert payload["token"] == "55555"
    assert payload["avg_price"] == pytest.approx(103.5)


# ─────────────────────────────────────────────── extra robustness
def test_orderbook_raises_falls_through_to_positions():
    """If order_book() raises, we still get a chance to recover via positions()."""
    bot, transitions = _make_bot(order_id="ORD-9")
    _force_pending_expired(bot)

    bot.broker.order_book.side_effect = RuntimeError("boom")
    bot.broker.positions.return_value = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY24500CE",
         "symboltoken": "55555", "netqty": "65", "avgnetprice": 100.25},
    ]

    bot._step_order_pending()

    assert bot.positions.open_position is not None
    assert bot._place_protective_legs.call_count == 1
    assert config.State.POSITION_OPEN in transitions


def test_both_broker_calls_raise_preserves_legacy_cancel_idle():
    """If BOTH order_book() and positions() raise, we must not crash and
    the legacy cancel/IDLE path must fire — no regression vs today."""
    bot, transitions = _make_bot(order_id="ORD-10")
    _force_pending_expired(bot)

    bot.broker.order_book.side_effect = RuntimeError("boom-1")
    bot.broker.positions.side_effect = RuntimeError("boom-2")

    bot._step_order_pending()

    assert bot.positions.open_position is None
    bot.broker.cancel_order.assert_called_once_with("ORD-10")
    assert config.State.IDLE in transitions


def test_orderbook_complete_but_missing_price_falls_through():
    """order_book shows COMPLETE but averageprice=0 → Fix A must NOT anchor
    to zero; fall through to positions() reconciliation."""
    bot, transitions = _make_bot(order_id="ORD-11")
    _force_pending_expired(bot)

    bot.broker.order_book.return_value = [
        {"orderid": "ORD-11", "status": "complete", "averageprice": 0.0},
    ]
    bot.broker.positions.return_value = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY24500CE",
         "symboltoken": "55555", "netqty": "65", "avgnetprice": 100.0},
    ]

    bot._step_order_pending()

    # Recovered via positions(), not via the zero-price order_book entry.
    assert bot.positions.open_position is not None
    assert bot.positions.open_position.entry_price == pytest.approx(100.0)


def test_positions_no_match_falls_back_to_cancel_idle():
    """positions() returns an unrelated NFO position (different token) →
    must NOT be adopted; legacy cancel/IDLE fires."""
    bot, transitions = _make_bot(order_id="ORD-12")
    _force_pending_expired(bot)

    bot.broker.order_book.return_value = []
    bot.broker.positions.return_value = [
        {"exchange": "NFO", "tradingsymbol": "OTHERSYMBOL",
         "symboltoken": "99999", "netqty": "65", "avgnetprice": 200.0},
    ]

    bot._step_order_pending()

    assert bot.positions.open_position is None
    bot.broker.cancel_order.assert_called_once_with("ORD-12")
    assert config.State.IDLE in transitions
