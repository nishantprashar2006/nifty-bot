"""
Regression tests for P0 execution-correctness fixes.

Covers the end-to-end contract-identity chain (P0-1 … P0-6/P0-7/P0-8):
    • Manual entries refresh the ATM contract just-in-time.
    • REST LTP fallback fires when the WebSocket has no quote.
    • LIVE fills use averageprice, not the order slot price.
    • Trade rows persist the full contract identity (symbol/token/strike/…).
    • Reset Breakers AND transition-to-SHUTDOWN cancel pending commands.
    • Daily profit lock has been removed; loss cap still fires.

These tests exercise the units in isolation (no real broker, no threads):
    SqliteLogger, PositionManager, PnlGuard.
The bot-level `_handle_manual_entry` / `_handle_fill` paths are covered by
targeted stub-based tests that mimic what the daemon does with a fake broker.
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("BOT_DB_PATH", tempfile.mktemp(suffix=".db"))

import config                                              # noqa: E402
from config import Direction                               # noqa: E402
from database.sqlite_logger import SqliteLogger            # noqa: E402
from risk.pnl_guard import PnlGuard                        # noqa: E402
from strategy.position_manager import (                    # noqa: E402
    PendingEntry, PositionManager,
)


# ─────────────────────────────────────────────── DB migration + persistence
def test_schema_has_new_contract_identity_columns():
    """P0-4: idempotent migration adds contract_symbol/token/strike/etc."""
    db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    with db._cursor() as cur:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(trades)").fetchall()}
    for expected in (
        "contract_symbol", "contract_token", "strike",
        "expiry", "option_type", "lot_size",
    ):
        assert expected in cols, f"missing column {expected} in trades"


def test_insert_trade_entry_persists_contract_identity():
    """P0-4: contract fields survive the DB round trip."""
    db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    db.insert_trade_entry(
        trade_id="T-abc",
        direction="CALL",
        qty=65,
        entry_price=124.7,
        contract_symbol="NIFTY24200PE",
        contract_token="47821",
        strike=24200.0,
        expiry="07FEB26",
        option_type="PE",
        lot_size=65,
        source="manual",
    )
    with db._cursor() as cur:
        row = cur.execute(
            "SELECT contract_symbol, contract_token, strike, expiry, option_type, lot_size "
            "FROM trades WHERE trade_id=?", ("T-abc",),
        ).fetchone()
    assert row == ("NIFTY24200PE", "47821", 24200.0, "07FEB26", "PE", 65)


# ─────────────────────────────────────────────── pending-command cancellation
def test_cancel_pending_commands_flips_all_pending_rows():
    """P0-6: cancel_pending_commands hits every non-terminal row."""
    db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    with db._cursor() as cur:
        for _ in range(3):
            cur.execute(
                "INSERT INTO commands(timestamp, action, payload, status) "
                "VALUES ('2026-02-06T09:20:00', 'manual_entry', '{}', 'pending')"
            )
        cur.execute(
            "INSERT INTO commands(timestamp, action, payload, status) "
            "VALUES ('2026-02-06T09:20:00', 'panic_exit', '{}', 'running')"
        )
        cur.execute(
            "INSERT INTO commands(timestamp, action, payload, status) "
            "VALUES ('2026-02-06T09:20:00', 'manual_entry', '{}', 'done')"
        )
    n = db.cancel_pending_commands(reason="test_cancel")
    assert n == 4, "expected 3 pending + 1 running to be cancelled"
    with db._cursor() as cur:
        rows = cur.execute(
            "SELECT status, result FROM commands ORDER BY id"
        ).fetchall()
    # first four are cancelled, last one remains 'done'
    statuses = [r[0] for r in rows]
    assert statuses == ["cancelled"] * 4 + ["done"]
    assert all(r[1] == "test_cancel" for r in rows[:4])
    # No 'done' row was clobbered
    assert rows[4][1] is None


def test_cancel_pending_commands_is_idempotent_when_empty():
    db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    assert db.cancel_pending_commands() == 0


# ─────────────────────────────────────────────── PnL guard (P0-7 / P0-8)
def test_profit_lock_completely_removed_no_shutdown_on_gain():
    """P0-7: PnlGuard must NOT breach on profit — profit lock is gone."""
    g = PnlGuard(daily_loss_cap=-2000)
    for pnl in (500, 1200, 3000, 10_000):
        g.add_realized(pnl)
        assert not g.evaluate().breached, (
            f"profit shutdown re-appeared at realized={g.realized_pnl}"
        )


def test_loss_cap_still_fires():
    """P0-8: loss cap still triggers SHUTDOWN — safety intact."""
    g = PnlGuard(daily_loss_cap=-2000)
    g.add_realized(-2100)
    v = g.evaluate()
    assert v.breached
    assert "loss_cap_hit" in v.reason


def test_pnl_guard_constructor_no_longer_accepts_profit_lock():
    """P0-7: single-arg constructor. Passing an old-style second positional
    argument would silently re-enable the profit lock; ensure it raises."""
    with pytest.raises(TypeError):
        PnlGuard(-1500, 3000)  # type: ignore[call-arg]


# ─────────────────────────────────────────────── contract identity in flow
def test_promote_to_open_carries_contract_identity():
    """P0-4: PendingEntry → OpenPosition preserves strike/expiry/type."""
    pm = PositionManager()
    p = PendingEntry(
        order_id="O1", direction=Direction.SHORT,
        contract_symbol="NIFTY24200PE", contract_token="47821",
        expected_price=125.0, lots=1, qty=65,
        target_price=163.0, stop_price=106.25,
        strike=24200.0, expiry="07FEB26", option_type="PE", lot_size=65,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    )
    pm.register_pending_entry(p)
    pos = pm.promote_to_open(fill_price=124.70)
    assert pos.contract_symbol == "NIFTY24200PE"
    assert pos.contract_token == "47821"
    assert pos.strike == 24200.0
    assert pos.expiry == "07FEB26"
    assert pos.option_type == "PE"
    assert pos.lot_size == 65
    # SL/TP anchor to fill price (P0-3 downstream — verified elsewhere too)
    assert pos.entry_price == pytest.approx(124.70)
    assert pos.stop_price == pytest.approx(124.70 * 0.85, rel=1e-6)
    assert pos.target_price == pytest.approx(124.70 * 1.30, rel=1e-6)


# ─────────────────────────────────────────────── LIVE avg_price preference
def test_handle_fill_uses_avg_price_not_slot_price():
    """P0-3: LIVE fills prefer OrderEvent.avg_price. Simulates a MARKET
    fill where the order-slot 'price' field is 0 and averageprice is 100.

    We import the bot class lazily and stub every collaborator so no real
    broker / websocket is instantiated — the goal is to exercise the fill
    path in isolation.
    """
    from broker.websocket_manager import OrderEvent

    # Build a stripped-down NiftyOptionsBot instance with just the fields
    # _handle_fill needs. Avoids `__init__` (which spins up a broker).
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._pending_source = "manual"
    bot._pending_engine = "smc"
    bot._pending_confidence = 72
    bot._pending_reasons = ["test"]
    bot.state = config.State.ORDER_PENDING
    bot._state_lock = __import__("threading").RLock()
    bot._place_protective_legs = MagicMock()

    bot.positions.register_pending_entry(PendingEntry(
        order_id="ORD-1", direction=Direction.LONG,
        contract_symbol="NIFTY24500CE", contract_token="55555",
        expected_price=100.0, lots=1, qty=65,
        target_price=130.0, stop_price=85.0,
        strike=24500.0, expiry="07FEB26", option_type="CE", lot_size=65,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    ))

    # MARKET fill: slot price = 0, averageprice = 100. Old code would have
    # anchored SL/TP to 0 → catastrophic. New code must use 100.
    ev = OrderEvent(
        order_id="ORD-1", status="complete",
        fill_price=0.0, avg_price=100.0, text="LIVE-MARKET",
        ts=0.0,
    )
    bot._handle_fill(ev)
    pos = bot.positions.open_position
    assert pos is not None, "position should have been promoted"
    assert pos.entry_price == pytest.approx(100.0)
    assert pos.stop_price == pytest.approx(85.0, rel=1e-6)
    assert pos.target_price == pytest.approx(130.0, rel=1e-6)


def test_handle_fill_falls_back_to_fill_price_when_avg_missing():
    """SIM path: force_order sets both fields equal. Test the fallback
    branch works when avg_price is 0 but fill_price is populated."""
    from broker.websocket_manager import OrderEvent
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._pending_source = "auto"
    bot._pending_engine = None
    bot._pending_confidence = None
    bot._pending_reasons = []
    bot.state = config.State.ORDER_PENDING
    bot._state_lock = __import__("threading").RLock()
    bot._place_protective_legs = MagicMock()

    bot.positions.register_pending_entry(PendingEntry(
        order_id="ORD-2", direction=Direction.LONG,
        contract_symbol="NIFTY24500CE", contract_token="55555",
        expected_price=100.0, lots=1, qty=65,
        target_price=130.0, stop_price=85.0,
        strike=24500.0, expiry="07FEB26", option_type="CE", lot_size=65,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    ))
    ev = OrderEvent(
        order_id="ORD-2", status="complete",
        fill_price=100.5, avg_price=0.0, text="SIM-fallback",
        ts=0.0,
    )
    bot._handle_fill(ev)
    pos = bot.positions.open_position
    assert pos is not None
    assert pos.entry_price == pytest.approx(100.5)


def test_handle_fill_aborts_when_both_prices_zero():
    """Defensive: broker returns neither field → don't anchor SL to zero."""
    from broker.websocket_manager import OrderEvent
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._pending_source = None
    bot._pending_engine = None
    bot._pending_confidence = None
    bot._pending_reasons = []
    bot.state = config.State.ORDER_PENDING
    bot._state_lock = __import__("threading").RLock()

    bot.positions.register_pending_entry(PendingEntry(
        order_id="ORD-3", direction=Direction.LONG,
        contract_symbol="X", contract_token="Y",
        expected_price=100.0, lots=1, qty=65,
        target_price=130.0, stop_price=85.0,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    ))
    ev = OrderEvent(order_id="ORD-3", status="complete",
                    fill_price=0.0, avg_price=0.0, text="corrupt",
                    ts=0.0)
    bot._handle_fill(ev)
    # Must NOT promote a zero-priced position
    assert bot.positions.open_position is None
    assert bot.state is config.State.FORCED_EXIT


# ─────────────────────────────────────────────── manual entry refresh (P0-1)
def test_manual_entry_calls_refresh_atm_before_picking_contract():
    """P0-1: _handle_manual_entry must refresh strikes on every click,
    regardless of whether _ce/_pe is already populated. Also assert that
    the contract used is the one written by _refresh_atm_contracts, not
    a stale value pre-existing on the instance."""
    from main import NiftyOptionsBot
    from data.option_selector import OptionContract

    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.state = config.State.IDLE
    bot._state_lock = __import__("threading").RLock()
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._trades_today = 0
    bot._consecutive_losses = 0
    bot._api_reject_count = 0
    bot.ws = None
    bot.pnl_guard = PnlGuard(-2000)

    # Stale contracts from "startup" (deep ITM)
    stale_ce = OptionContract(symbol="STALE_CE", token="0000", strike=24000,
                              expiry="07FEB26", option_type="CE", lot_size=65)
    stale_pe = OptionContract(symbol="STALE_PE", token="0000", strike=24500,
                              expiry="07FEB26", option_type="PE", lot_size=65)
    bot._ce, bot._pe = stale_ce, stale_pe

    # Fresh picks the refresh should write in
    fresh_ce = OptionContract(symbol="FRESH_CE", token="1111", strike=24250,
                              expiry="07FEB26", option_type="CE", lot_size=65)
    fresh_pe = OptionContract(symbol="FRESH_PE", token="2222", strike=24200,
                              expiry="07FEB26", option_type="PE", lot_size=65)

    refresh_calls = {"n": 0}

    def _fake_refresh():
        refresh_calls["n"] += 1
        bot._ce, bot._pe = fresh_ce, fresh_pe

    bot._refresh_atm_contracts = _fake_refresh
    # Stub _place_entry so we can capture what contract was chosen and
    # short-circuit before broker interaction.
    captured = {}

    def _fake_place_entry(direction, contract, **kw):
        captured["direction"] = direction
        captured["contract"] = contract
        return True

    bot._place_entry = _fake_place_entry
    bot._pending_engine = None
    bot._pending_confidence = None
    bot._pending_reasons = []

    ok, msg = bot._handle_manual_entry(Direction.LONG, engine="indicator")
    assert ok, msg
    assert refresh_calls["n"] == 1, "refresh must fire exactly once per click"
    assert captured["contract"].symbol == "FRESH_CE"
    assert captured["contract"].token == "1111"


# ─────────────────────────────────────────────── REST LTP fallback (P0-2)
def test_place_entry_rest_ltp_fallback_when_ws_quote_missing(monkeypatch):
    """P0-2: when the WS has no quote for the freshly-picked contract, the
    bot must call broker.ltp() once to bootstrap the premium — and use it
    to anchor SL/TP. Verified in SIM (where a wrong premium historically
    caused a fake ₹100 fallback fill)."""
    from main import NiftyOptionsBot
    from data.option_selector import OptionContract

    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.state = config.State.IDLE
    bot._state_lock = __import__("threading").RLock()
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._last_option_quote = {}   # ← WS has no quote for the new token
    bot._pending_source = None
    bot._pending_engine = None
    bot._pending_confidence = None
    bot._pending_reasons = []
    bot._effective_lots = 1
    bot.sizer = MagicMock()
    bot.sizer.premium_spike_guard = MagicMock(return_value=1)
    bot.broker = MagicMock()
    bot.broker.get_net_available_cash = MagicMock(return_value=200_000.0)
    bot.broker.place_order = MagicMock(return_value="ORD-REST")
    # REST fallback returns a real premium
    bot.broker.ltp = MagicMock(return_value=124.7)
    bot.ws = MagicMock()

    # Force SIM mode so the synthesised fill path runs
    monkeypatch.setattr(config, "SIMULATE_ORDERS", True)
    monkeypatch.setattr(config, "ENTRY_ORDER_TYPE", "MARKET")

    contract = OptionContract(symbol="NIFTY24200PE", token="47821",
                              strike=24200, expiry="07FEB26",
                              option_type="PE", lot_size=65)
    ok = bot._place_entry(
        Direction.SHORT, contract,
        sl_pts=0.0, tp_pts=0.0,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
        source="manual",
    )
    assert ok, "SIM entry should succeed via REST fallback"
    bot.broker.ltp.assert_called_once_with("NFO", "NIFTY24200PE", "47821")
    # force_order must have been called with the REST-sourced premium as
    # the synthesised fill price, not the old hard-coded ₹100 fallback.
    args, _ = bot.ws.force_order.call_args
    ev = args[0]
    assert ev.fill_price == pytest.approx(124.7)
    assert ev.avg_price == pytest.approx(124.7)


# ─────────────────────────────────────────────── daily-trade cap intact
def test_max_trades_daily_still_triggers_shutdown():
    """P0-8: MAX_TRADES_DAILY discipline still fires the fifth trade block."""
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot._trades_today = config.MAX_TRADES_DAILY   # 4 by default
    bot._api_reject_count = 0
    bot.ws = None
    bot.pnl_guard = PnlGuard(-2000)
    assert bot._trip_circuit_breakers() == "max_trades_daily"
