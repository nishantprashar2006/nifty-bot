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
    # v3.0 — SL = floor(fill − 6), TP = floor(fill + 12)
    assert pos.stop_price == 118.0
    assert pos.target_price == 136.0


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
    # v3.0 — SL = floor(100 − 6) = 94, TP = floor(100 + 12) = 112
    assert pos.stop_price == 94.0
    assert pos.target_price == 112.0


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
    # v2.3 P3 — bypass 14:55 entry cutoff for test independence.
    bot._in_entry_window = lambda: True

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



# ─────────────────────────────── P0-Q1: WebSocket resubscribe on strike change
def test_refresh_atm_resubscribes_ws_on_token_change():
    """P0-Q1: when `_refresh_atm_contracts` picks a strike whose token
    isn't in the current WS subscription set, the bot must call
    ws.resubscribe(...) with the fresh token list. This is the fix for
    the LTP-freeze regression."""
    from main import NiftyOptionsBot
    from data.option_selector import OptionContract

    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot._state_lock = __import__("threading").RLock()
    bot.state = config.State.IDLE
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._last_option_quote = {}

    # Startup subs: old CE/PE tokens
    OLD_CE = OptionContract("OLD_CE", "1111", 24000, "07FEB26", "CE", 65)
    OLD_PE = OptionContract("OLD_PE", "2222", 24100, "07FEB26", "PE", 65)
    NEW_CE = OptionContract("NEW_CE", "9999", 24200, "07FEB26", "CE", 65)
    NEW_PE = OptionContract("NEW_PE", "8888", 24250, "07FEB26", "PE", 65)
    bot._ce, bot._pe = OLD_CE, OLD_PE

    bot.broker = MagicMock()
    bot.broker.ltp = MagicMock(return_value=24225.0)
    bot.option_selector = MagicMock()
    bot.option_selector.select_atm = MagicMock(return_value=(NEW_CE, NEW_PE))

    resubscribed = {"calls": [], "returns": []}

    class FakeWS:
        def resubscribe(self, subs):
            resubscribed["calls"].append(subs)
            return True
    bot.ws = FakeWS()

    bot._refresh_atm_contracts()

    assert bot._ce.token == "9999"
    assert bot._pe.token == "8888"
    assert len(resubscribed["calls"]) == 1, "resubscribe must fire exactly once"
    # The new sub list must include both new tokens
    flat = []
    for group in resubscribed["calls"][0]:
        flat.extend(group.get("tokens", []))
    assert "9999" in flat and "8888" in flat


def test_refresh_atm_skips_resubscribe_when_tokens_unchanged():
    """Idempotency: if strikes didn't change, don't hammer the WS."""
    from main import NiftyOptionsBot
    from data.option_selector import OptionContract

    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot._state_lock = __import__("threading").RLock()
    bot.state = config.State.IDLE
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._last_option_quote = {}
    SAME_CE = OptionContract("CE", "1111", 24200, "07FEB26", "CE", 65)
    SAME_PE = OptionContract("PE", "2222", 24250, "07FEB26", "PE", 65)
    bot._ce, bot._pe = SAME_CE, SAME_PE

    bot.broker = MagicMock()
    bot.broker.ltp = MagicMock(return_value=24225.0)
    bot.option_selector = MagicMock()
    bot.option_selector.select_atm = MagicMock(return_value=(SAME_CE, SAME_PE))

    called = {"n": 0}

    class FakeWS:
        def resubscribe(self, subs):
            called["n"] += 1
            return True
    bot.ws = FakeWS()

    bot._refresh_atm_contracts()
    assert called["n"] == 0, "resubscribe fired despite unchanged strikes"


# ─────────────────────────────── P0-Q1: LTP seed at entry
def test_place_entry_seeds_last_option_quote_and_live_state(monkeypatch):
    """P0-Q1 regression: after a successful `_place_entry`, both the
    in-memory `_last_option_quote[token]` AND the persisted
    `live_quotes.option_ltp / option_ltp_token` must reflect THIS
    contract's premium — so the very first dashboard frame after entry
    is never a stale value from a previous scenario."""
    from main import NiftyOptionsBot
    from data.option_selector import OptionContract

    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.state = config.State.IDLE
    bot._state_lock = __import__("threading").RLock()
    bot.positions = PositionManager()
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._last_option_quote = {}
    bot._pending_source = None
    bot._pending_engine = None
    bot._pending_confidence = None
    bot._pending_reasons = []
    bot._effective_lots = 1
    bot.sizer = MagicMock()
    bot.sizer.premium_spike_guard = MagicMock(return_value=1)
    bot.broker = MagicMock()
    bot.broker.get_net_available_cash = MagicMock(return_value=200_000.0)
    bot.broker.place_order = MagicMock(return_value="ORD-SEED")
    bot.broker.ltp = MagicMock(return_value=142.0)
    bot.vix = MagicMock()
    bot.vix.value = None
    bot.ws = MagicMock()
    monkeypatch.setattr(config, "SIMULATE_ORDERS", True)
    monkeypatch.setattr(config, "ENTRY_ORDER_TYPE", "MARKET")

    contract = OptionContract("NIFTY24500CE", "42424",
                              24500, "07FEB26", "CE", 65)
    ok = bot._place_entry(
        Direction.LONG, contract,
        sl_pts=0.0, tp_pts=0.0,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
        source="manual",
    )
    assert ok

    # In-memory seed
    q = bot._last_option_quote.get("42424")
    assert q is not None, "quote seed missing"
    assert q["ltp"] == pytest.approx(142.0)
    assert q["source"] == "seed"

    # Persisted seed in bot_state.live_quotes
    import json as _json
    row = bot.db.get_state("live_quotes")
    assert row is not None
    payload = _json.loads(row[0])
    assert payload["option_ltp"] == pytest.approx(142.0)
    assert payload["option_ltp_token"] == "42424"
    assert payload["option_ltp_ts"] > 0


# ─────────────────────────────── P0-Q1: tick guard on token match
def test_on_tick_ignores_option_ltp_when_token_doesnt_match_open_pos():
    """A tick for a NON-open-position option token must not overwrite
    `live_quotes.option_ltp` — otherwise cross-contract bleed causes the
    dashboard to show a stranger's LTP for the currently-open position."""
    from main import NiftyOptionsBot
    from broker.websocket_manager import Tick
    from strategy.position_manager import OpenPosition
    from datetime import datetime, timezone

    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot._last_option_quote = {}
    from data.candle_manager import CandleManager
    bot.candles = CandleManager()
    bot.positions = PositionManager()
    bot.vix = MagicMock()
    bot.vix.value = None

    # Open position on token X — seed the state
    bot.positions._open = OpenPosition(
        trade_id="T1", direction=Direction.LONG,
        contract_symbol="X_CE", contract_token="X",
        qty=65, lots=1, entry_price=100.0,
        entry_ts=datetime.now(timezone.utc),
        target_price=130.0, stop_price=85.0,
        strike=24000, expiry="07FEB26", option_type="CE", lot_size=65,
    )
    bot._update_live_state(option_ltp=100.0, option_token="X")

    # A tick arrives for token Y (a completely different contract)
    stray = Tick(token="Y", ltp=63.30, volume=0, oi=0, bid=63.3, ask=63.4, ts=0.0)
    bot._on_tick(stray)

    # live_quotes.option_ltp MUST still be 100.0 (from token X seed).
    import json as _json
    payload = _json.loads(bot.db.get_state("live_quotes")[0])
    assert payload["option_ltp"] == pytest.approx(100.0), (
        f"cross-contract bleed: {payload}"
    )
    assert payload["option_ltp_token"] == "X"
    # But the local quote cache DOES record token Y's LTP (for future use).
    assert bot._last_option_quote["Y"]["ltp"] == pytest.approx(63.30)


# ─────────────────────────────── P0-Q1: WS health snapshot
def test_ws_manager_health_shape():
    """WS health dict must expose the fields the dashboard renders."""
    from broker.websocket_manager import WebSocketManager
    ws = WebSocketManager(
        feed_token="x", client_id="y", jwt="z",
        token_subscriptions=[{"exchangeType": 2, "tokens": ["1111", "2222"]}],
    )
    h = ws.health()
    for k in ("connected", "last_tick_ts", "seconds_since_last_tick",
              "reconnect_failures", "subscribed_tokens", "subscribed_count"):
        assert k in h, f"ws_health missing key {k}"
    assert h["subscribed_count"] == 2
    assert set(h["subscribed_tokens"]) == {"1111", "2222"}
    assert h["connected"] is False   # sdk_ws is None outside a live connect


def test_ws_manager_resubscribe_caches_tokens_when_socket_down():
    """resubscribe() must always update `_token_subs` so the next
    reconnect picks up the fresh list, even if the SDK object is not
    currently connected."""
    from broker.websocket_manager import WebSocketManager
    ws = WebSocketManager(
        feed_token="x", client_id="y", jwt="z",
        token_subscriptions=[{"exchangeType": 2, "tokens": ["AAA"]}],
    )
    new = [{"exchangeType": 2, "tokens": ["BBB", "CCC"]}]
    ok = ws.resubscribe(new)
    assert ok is False   # no live sdk_ws
    assert ws.subscribed_tokens() == ["BBB", "CCC"]


# ─────────────────────────────── P0-Q2: no periodic REST hammer
def test_no_periodic_atm_refresh_attr_on_bot():
    """P0-Q2: the periodic refresh throttle var was removed; ensure
    nothing accidentally re-introduces the 10-second REST timer."""
    import main
    src = open(main.__file__).read()
    assert "_last_atm_publish_ts" not in src, (
        "periodic ATM publish throttle re-appeared — REST hammer regression"
    )
    # And the drainable action must exist for the on-demand path
    assert 'action == "refresh_atm"' in src


# ─────────────────────────────── refresh_atm command handler
def test_refresh_atm_command_calls_refresh():
    """The `refresh_atm` action drained from the command queue must call
    `_refresh_atm_contracts`."""
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot._state_lock = __import__("threading").RLock()
    bot.state = config.State.IDLE
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    called = {"n": 0}
    bot._refresh_atm_contracts = lambda: called.update(n=called["n"] + 1)

    # Enqueue then drain
    with bot.db._cursor() as cur:
        cur.execute(
            "INSERT INTO commands(timestamp, action, payload, status) "
            "VALUES ('t','refresh_atm','{}','pending')"
        )
    bot._drain_command_queue()
    assert called["n"] == 1


# ─────────────────────────────────────────── v1.9 — protection telemetry
def test_promote_to_open_freezes_initial_sl_and_tp():
    """v1.9: `initial_stop_price` and `initial_target_price` must be
    frozen at promotion time so audits can compare against them later."""
    pm = PositionManager()
    p = PendingEntry(
        order_id="O", direction=Direction.LONG,
        contract_symbol="X", contract_token="1",
        expected_price=100.0, lots=1, qty=65,
        target_price=130.0, stop_price=85.0,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    )
    pm.register_pending_entry(p)
    pos = pm.promote_to_open(fill_price=100.0)
    # v3.0 — SL = 94, TP = 112 (floored fixed points)
    assert pos.initial_stop_price == 94.0
    assert pos.initial_target_price == 112.0
    assert pos.trail_bumps == 0
    assert pos.highest_ltp_seen == pytest.approx(100.0)
    assert pos.lowest_ltp_seen == pytest.approx(100.0)


def test_maybe_trail_stop_increments_bump_counter():
    """v3.0: trailing REMOVED — maybe_trail_stop always returns None and
    trail_bumps stays 0 for the trade's entire lifecycle."""
    pm = PositionManager()
    p = PendingEntry(
        order_id="O", direction=Direction.LONG,
        contract_symbol="X", contract_token="1",
        expected_price=100.0, lots=1, qty=65,
        target_price=112.0, stop_price=94.0,
    )
    pm.register_pending_entry(p)
    pos = pm.promote_to_open(fill_price=100.0)
    for premium in (110.0, 115.0, 120.0, 130.0):
        assert pm.maybe_trail_stop(current_premium=premium) is None
    assert pos.trail_bumps == 0
    # Stop price is frozen at initial value
    assert pos.stop_price == 94.0


def test_finalize_exit_uses_trailing_stop_label_when_bumped():
    """v1.9: an SL exit after trail bumps must be labelled TRAILING_STOP,
    not STOP_LOSS. This is the exact regression the T-8261be7125 audit
    would have solved in one query."""
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot._state_lock = __import__("threading").RLock()
    bot.state = config.State.POSITION_OPEN
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot.positions = PositionManager()
    bot.pnl_guard = PnlGuard(-2000)
    bot._trades_today = 0
    bot._consecutive_losses = 0
    bot._cooldown_until = None
    bot.broker = MagicMock()
    bot.broker.cancel_order = MagicMock()

    # Simulate a promoted position that has trailed twice
    p = PendingEntry(
        order_id="O", direction=Direction.LONG,
        contract_symbol="NIFTY24500CE", contract_token="111",
        expected_price=84.10, lots=1, qty=65,
        target_price=109.33, stop_price=71.49,
        strike=24500, expiry="10JUL26", option_type="CE", lot_size=65,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    )
    bot.positions.register_pending_entry(p)
    pos = bot.positions.promote_to_open(84.10)
    # Simulate one trail bump: stop_price moved beyond initial
    pos.stop_price = 79.895
    pos.trail_anchor = 92.51
    pos.trail_bumps = 1

    # Insert a trade row so update_trade_exit has something to UPDATE
    bot.db.insert_trade_entry(
        trade_id=pos.trade_id, direction="CALL", qty=pos.qty,
        entry_price=pos.entry_price, source="manual",
        contract_symbol=pos.contract_symbol, contract_token=pos.contract_token,
    )
    bot._finalize_exit(exit_price=79.85, was_stop=True)

    with bot.db._cursor() as cur:
        row = dict(zip(
            [c[0] for c in cur.execute(
                "SELECT exit_reason, exit_trigger, trail_bumps, final_stop_price, "
                "initial_sl_price FROM trades WHERE trade_id=?", (pos.trade_id,),
            ).description],
            cur.execute(
                "SELECT exit_reason, exit_trigger, trail_bumps, final_stop_price, "
                "initial_sl_price FROM trades WHERE trade_id=?", (pos.trade_id,),
            ).fetchone(),
        ))
    assert row["exit_reason"] == "TRAILING_STOP"
    assert row["exit_trigger"] == "TRAILING_STOP"
    assert row["trail_bumps"] == 1
    assert row["final_stop_price"] == pytest.approx(79.895)
    # v3.0 — initial SL = floor(fill − 6) = floor(84.10 − 6) = 78
    assert row["initial_sl_price"] == pytest.approx(78.0)


def test_finalize_exit_keeps_stop_loss_label_when_never_bumped():
    """Complement to the above: SL exit without any trail bump stays
    labelled STOP_LOSS. Ensures we haven't broken the initial-SL case."""
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot._state_lock = __import__("threading").RLock()
    bot.state = config.State.POSITION_OPEN
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot.positions = PositionManager()
    bot.pnl_guard = PnlGuard(-2000)
    bot._trades_today = 0
    bot._consecutive_losses = 0
    bot._cooldown_until = None
    bot.broker = MagicMock()
    bot.broker.cancel_order = MagicMock()

    p = PendingEntry(
        order_id="O", direction=Direction.LONG,
        contract_symbol="X", contract_token="1",
        expected_price=100.0, lots=1, qty=65,
        target_price=130.0, stop_price=85.0,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    )
    bot.positions.register_pending_entry(p)
    pos = bot.positions.promote_to_open(100.0)
    # trail_bumps stays 0, stop stays at initial
    bot.db.insert_trade_entry(
        trade_id=pos.trade_id, direction="CALL", qty=65, entry_price=100.0,
        source="manual",
    )
    bot._finalize_exit(exit_price=85.0, was_stop=True)
    with bot.db._cursor() as cur:
        (reason, bumps) = cur.execute(
            "SELECT exit_reason, trail_bumps FROM trades WHERE trade_id=?",
            (pos.trade_id,),
        ).fetchone()
    assert reason == "STOP_LOSS"
    assert bumps == 0


# ─────────────────────────────────────────── v1.9 — stale-quote breaker
def test_stale_quote_breaker_transitions_to_forced_exit(monkeypatch):
    """v1.9 P0: if the WS hasn't ticked the open position's token for
    > STALE_QUOTE_EXIT_SEC while the position has been held for at least
    that long, the bot must fire a STALE_FEED forced exit rather than
    sit unprotected."""
    from main import NiftyOptionsBot
    from strategy.position_manager import OpenPosition
    from datetime import datetime, timezone, timedelta, time as _time

    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot._state_lock = __import__("threading").RLock()
    bot.state = config.State.POSITION_OPEN
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot.positions = PositionManager()
    bot._last_option_quote = {}
    bot._exit_reason_hint = None
    bot._ltp_history = {}
    bot._pending_source = None

    monkeypatch.setattr(config, "STALE_QUOTE_EXIT_SEC", 20)
    # Prevent the intraday square-off from firing during unit tests
    monkeypatch.setattr(config, "INTRADAY_SQUARE_OFF", _time(23, 59))
    # Position opened 60s ago; WS quote is 90s old
    now = datetime.now(timezone.utc)
    bot.positions._open = OpenPosition(
        trade_id="T1", direction=Direction.LONG,
        contract_symbol="X", contract_token="TOKX",
        qty=65, lots=1, entry_price=100.0,
        entry_ts=now - timedelta(seconds=60),
        target_price=130.0, stop_price=85.0,
    )
    import time as _t
    bot._last_option_quote["TOKX"] = {
        "ltp": 100.0, "ts": _t.time() - 90, "source": "ws",
    }
    bot._step_position_open()
    assert bot.state is config.State.FORCED_EXIT
    assert bot._exit_reason_hint == "STALE_FEED"


def test_stale_quote_breaker_skips_when_disabled(monkeypatch):
    """STALE_QUOTE_EXIT_SEC = 0 disables the check (dev / off-hours)."""
    from main import NiftyOptionsBot
    from strategy.position_manager import OpenPosition
    from datetime import datetime, timezone, timedelta, time as _time

    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot._state_lock = __import__("threading").RLock()
    bot.state = config.State.POSITION_OPEN
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot.positions = PositionManager()
    bot._last_option_quote = {}
    bot._exit_reason_hint = None
    bot._ltp_history = {}
    bot._pending_source = None

    monkeypatch.setattr(config, "STALE_QUOTE_EXIT_SEC", 0)   # disabled
    # Prevent the intraday square-off from firing during unit tests
    monkeypatch.setattr(config, "INTRADAY_SQUARE_OFF", _time(23, 59))
    now = datetime.now(timezone.utc)
    bot.positions._open = OpenPosition(
        trade_id="T1", direction=Direction.LONG,
        contract_symbol="X", contract_token="TOKX",
        qty=65, lots=1, entry_price=100.0,
        entry_ts=now - timedelta(seconds=30),
        target_price=130.0, stop_price=85.0,
    )
    bot._last_option_quote["TOKX"] = {"ltp": 100.0, "ts": 0.0, "source": "ws"}
    bot._step_position_open()
    # Must NOT flip to FORCED_EXIT
    assert bot.state is config.State.POSITION_OPEN


# ─────────────────────────────────────────── v1.9 — live_position snapshot
def test_live_position_snapshot_persists_and_restores_trail_anchor():
    """v1.9 P1: after a trail bump we snapshot trail_anchor + bumps into
    bot_state.live_position. A subsequent restore must give us BACK the
    same trail_anchor / bumps / stop_price."""
    from main import NiftyOptionsBot
    bot = NiftyOptionsBot.__new__(NiftyOptionsBot)
    bot.db = SqliteLogger(tempfile.mktemp(suffix=".db"))
    bot.positions = PositionManager()

    p = PendingEntry(
        order_id="O", direction=Direction.LONG,
        contract_symbol="X", contract_token="TOKZ",
        expected_price=100.0, lots=1, qty=65,
        target_price=130.0, stop_price=85.0,
        sl_pct=0.15, tp_pct=0.30, trail_step_pct=0.10,
    )
    bot.positions.register_pending_entry(p)
    pos = bot.positions.promote_to_open(100.0)
    pos.stop_price = 95.0
    pos.trail_anchor = 110.0
    pos.trail_bumps = 1
    pos.highest_ltp_seen = 118.0

    bot._persist_live_position_state()

    row = bot.db.get_state("live_position")
    assert row is not None
    import json
    snap = json.loads(row[0])
    assert snap["trade_id"] == pos.trade_id
    assert snap["contract_token"] == "TOKZ"
    assert snap["stop_price"] == pytest.approx(95.0)
    assert snap["trail_anchor"] == pytest.approx(110.0)
    assert snap["trail_bumps"] == 1
    assert snap["highest_ltp_seen"] == pytest.approx(118.0)



# ─────────────────────────────────────────── v1.10 — Execution Timeline
def test_timeline_logger_schema_and_write():
    """v1.10: TimelineLogger creates the events table and INSERTs rows
    without raising even if some payload keys are exotic."""
    from execution_timeline import TimelineLogger, Event
    db_path = tempfile.mktemp(suffix=".db")
    tl = TimelineLogger(db_path)
    tl.log("T-abc", Event.ENTRY_CLICK, "click", {"direction": "CALL"})
    tl.log("T-abc", Event.CONTRACT_SELECTED, "picked", {"symbol": "NIFTY24500CE"})
    events = tl.timeline_for("T-abc")
    assert len(events) == 2
    assert events[0]["event_type"] == "ENTRY_CLICK"
    assert events[0]["payload"]["direction"] == "CALL"
    assert events[1]["message"] == "picked"


def test_timeline_logger_never_raises_on_bad_payload():
    """log() must swallow exceptions — trading loop must never crash
    because of a logging error."""
    from execution_timeline import TimelineLogger, Event
    tl = TimelineLogger(tempfile.mktemp(suffix=".db"))
    # Non-JSON-serializable payload — should be logged & suppressed
    class NotJSON:
        pass
    tl.log("T-x", Event.NOTE, "with weird payload", {"obj": NotJSON()})
    # Timeline still returns an empty list; no exception propagated
    events = tl.timeline_for("T-x")
    assert isinstance(events, list)


def test_timeline_rekey_session_reassigns_pre_fill_events():
    """v1.10: events written under the session key before the fill must be
    rewritten to the real trade_id, giving the UI one contiguous timeline."""
    from execution_timeline import TimelineLogger, Event, new_session_id
    tl = TimelineLogger(tempfile.mktemp(suffix=".db"))
    sess = new_session_id()
    tl.log(sess, Event.ENTRY_CLICK, "click")
    tl.log(sess, Event.CONTRACT_SELECTED, "picked")
    # simulate fill → learn trade_id
    tl.rekey_session(sess, "T-real")
    # Post-fill events go under real trade_id
    tl.log("T-real", Event.ENTRY_FILL, "filled")
    events = tl.timeline_for("T-real")
    assert len(events) == 3
    assert [e["event_type"] for e in events] == [
        "ENTRY_CLICK", "CONTRACT_SELECTED", "ENTRY_FILL",
    ]
    assert tl.timeline_for(sess) == []


def test_timeline_ordered_chronologically():
    """Events must return in INSERT order (which is time order)."""
    from execution_timeline import TimelineLogger, Event
    tl = TimelineLogger(tempfile.mktemp(suffix=".db"))
    for i, et in enumerate([
        Event.ENTRY_CLICK, Event.ATM_REFRESH, Event.CONTRACT_SELECTED,
        Event.ORDER_SUBMIT, Event.ENTRY_FILL, Event.SL_PLACED,
        Event.TP_PLACED, Event.TRAIL_BUMP, Event.EXIT_FILL,
    ]):
        tl.log("T-order", et, f"step {i}")
    ids = [e["id"] for e in tl.timeline_for("T-order")]
    assert ids == sorted(ids), "events must be returned in id (chronological) order"

