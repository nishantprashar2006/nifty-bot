"""
database/sqlite_logger.py
=========================
Manages the four streamlined indexed tracking tables:

  1. trades             — fills + exits with PnL
  2. indicators         — periodic snapshot of indicator state
  3. state_transitions  — every FSM hop
  4. equity_curve       — daily mark-to-market peak / drawdown trail

Pure stdlib `sqlite3`. Thread-safe via a single lock + check_same_thread=False.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteLogger:
    """Lightweight indexed SQLite logger for the trading bot."""

    _SCHEMA = [
        """
        CREATE TABLE IF NOT EXISTS trades (
            trade_id     TEXT PRIMARY KEY,
            entry_time   TEXT NOT NULL,
            exit_time    TEXT,
            direction    TEXT NOT NULL,
            qty          INTEGER NOT NULL,
            entry_price  REAL NOT NULL,
            exit_price   REAL,
            pnl          REAL,
            exit_reason  TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades(entry_time)",
        """
        CREATE TABLE IF NOT EXISTS indicators (
            timestamp   TEXT NOT NULL,
            ema9        REAL,
            ema21       REAL,
            ema20_15m   REAL,
            ema50_15m   REAL,
            rsi         REAL,
            adx         REAL,
            vwap        REAL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_indicators_ts ON indicators(timestamp)",
        """
        CREATE TABLE IF NOT EXISTS state_transitions (
            timestamp   TEXT NOT NULL,
            old_state   TEXT NOT NULL,
            new_state   TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_state_ts ON state_transitions(timestamp)",
        """
        CREATE TABLE IF NOT EXISTS equity_curve (
            timestamp       TEXT PRIMARY KEY,
            current_equity  REAL NOT NULL,
            peak_equity     REAL NOT NULL,
            drawdown_pct    REAL NOT NULL,
            effective_lots  INTEGER NOT NULL,
            trading_mode    TEXT NOT NULL DEFAULT 'paper'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_curve(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_equity_mode ON equity_curve(trading_mode)",
        """
        CREATE TABLE IF NOT EXISTS commands (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            action      TEXT NOT NULL,
            payload     TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'pending',
            result      TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status)",
        """
        CREATE TABLE IF NOT EXISTS bot_state (
            key      TEXT PRIMARY KEY,
            value    TEXT NOT NULL,
            updated  TEXT NOT NULL
        )
        """,
        # v1.14 — Phase Y broker API audit log. Every call to the five
        # audited methods (place_order / modify_order / cancel_order /
        # order_book / positions) writes a row here for diagnostic
        # visibility. Never read by any trading logic — read-only from
        # the /api/bot/broker_audit endpoint.
        """
        CREATE TABLE IF NOT EXISTS broker_audit_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            method            TEXT NOT NULL,
            request_ts        REAL NOT NULL,
            response_ts       REAL NOT NULL,
            latency_ms        INTEGER NOT NULL,
            ok                INTEGER NOT NULL,
            error_code        TEXT,
            error_message     TEXT,
            broker_order_id   TEXT,
            exchange_order_id TEXT,
            request_summary   TEXT,
            response_summary  TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_broker_audit_method ON broker_audit_log(method)",
        "CREATE INDEX IF NOT EXISTS idx_broker_audit_req_ts ON broker_audit_log(request_ts)",
    ]

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            for stmt in self._SCHEMA:
                cur.execute(stmt)
            # Idempotent column adds for legacy DBs
            for ddl in (
                "ALTER TABLE equity_curve ADD COLUMN trading_mode TEXT NOT NULL DEFAULT 'paper'",
                "ALTER TABLE trades ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'",
                # PART 3 — manual-entry metadata
                "ALTER TABLE trades ADD COLUMN engine TEXT",
                "ALTER TABLE trades ADD COLUMN confidence INTEGER",
                "ALTER TABLE trades ADD COLUMN reasons TEXT",
                "ALTER TABLE trades ADD COLUMN sl_price REAL",
                "ALTER TABLE trades ADD COLUMN tp_price REAL",
                # P0-4 — full option-contract identity per trade so dashboard,
                # DB, broker, and execution pipeline can never diverge.
                "ALTER TABLE trades ADD COLUMN contract_symbol TEXT",
                "ALTER TABLE trades ADD COLUMN contract_token TEXT",
                "ALTER TABLE trades ADD COLUMN strike REAL",
                "ALTER TABLE trades ADD COLUMN expiry TEXT",
                "ALTER TABLE trades ADD COLUMN option_type TEXT",
                "ALTER TABLE trades ADD COLUMN lot_size INTEGER",
                # v1.9 — protection-state telemetry
                "ALTER TABLE trades ADD COLUMN initial_sl_price REAL",
                "ALTER TABLE trades ADD COLUMN initial_tp_price REAL",
                "ALTER TABLE trades ADD COLUMN final_stop_price REAL",
                "ALTER TABLE trades ADD COLUMN trail_bumps INTEGER",
                "ALTER TABLE trades ADD COLUMN highest_ltp REAL",
                "ALTER TABLE trades ADD COLUMN lowest_ltp REAL",
                "ALTER TABLE trades ADD COLUMN exit_trigger TEXT",
                # v2.4 — trigger attribution (MANUAL / CONFIDENCE_THRESHOLD / BOS_STRUCTURE)
                "ALTER TABLE trades ADD COLUMN trigger_reason TEXT",
            ):
                try:
                    cur.execute(ddl)
                except sqlite3.OperationalError:
                    pass
            cur.close()

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    # ------------------------------------------------------------------ writes
    def log_state_transition(self, old_state: str, new_state: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO state_transitions(timestamp, old_state, new_state) "
                "VALUES (?, ?, ?)",
                (_utc_iso(), old_state, new_state),
            )

    def log_indicator_snapshot(self, snap: dict[str, Any]) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO indicators(timestamp, ema9, ema21, ema20_15m, "
                "ema50_15m, rsi, adx, vwap) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snap.get("timestamp", _utc_iso()),
                    snap.get("ema9"),
                    snap.get("ema21"),
                    snap.get("ema20_15m"),
                    snap.get("ema50_15m"),
                    snap.get("rsi"),
                    snap.get("adx"),
                    snap.get("vwap"),
                ),
            )

    def insert_trade_entry(
        self,
        trade_id: str,
        direction: str,
        qty: int,
        entry_price: float,
        entry_time: Optional[str] = None,
        source: str = "auto",
        engine: Optional[str] = None,
        confidence: Optional[int] = None,
        reasons: Optional[list] = None,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        # P0-4: full contract identity, mandatory going forward
        contract_symbol: Optional[str] = None,
        contract_token: Optional[str] = None,
        strike: Optional[float] = None,
        expiry: Optional[str] = None,
        option_type: Optional[str] = None,
        lot_size: Optional[int] = None,
        trigger_reason: Optional[str] = None,
    ) -> None:
        import json as _json
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO trades(trade_id, entry_time, direction, qty, "
                "entry_price, source, engine, confidence, reasons, sl_price, tp_price, "
                "contract_symbol, contract_token, strike, expiry, option_type, lot_size, "
                "trigger_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_id, entry_time or _utc_iso(), direction, qty, entry_price,
                    source, engine, confidence,
                    _json.dumps(reasons) if reasons else None,
                    sl_price, tp_price,
                    contract_symbol, contract_token, strike, expiry, option_type, lot_size,
                    trigger_reason,
                ),
            )

    def cancel_pending_commands(self, reason: str = "cancelled") -> int:
        """P0-6: mark every currently-pending command as cancelled.

        Called both when the FSM transitions into SHUTDOWN and when the user
        clicks Reset Breakers. Guarantees a queued manual_entry cannot fire
        after the state has changed under it.

        Returns the number of rows cancelled.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE commands SET status='cancelled', result=? "
                "WHERE status IN ('pending','running')",
                (reason,),
            )
            return int(cur.rowcount or 0)

    def update_trade_exit(
        self,
        trade_id: str,
        exit_price: float,
        pnl: float,
        exit_reason: str,
        exit_time: Optional[str] = None,
        # v1.9 telemetry (optional; only set from the bot's exit path)
        final_stop_price: Optional[float] = None,
        trail_bumps: Optional[int] = None,
        highest_ltp: Optional[float] = None,
        lowest_ltp: Optional[float] = None,
        exit_trigger: Optional[str] = None,
        initial_sl_price: Optional[float] = None,
        initial_tp_price: Optional[float] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE trades SET exit_time=?, exit_price=?, pnl=?, exit_reason=?, "
                "final_stop_price=COALESCE(?, final_stop_price), "
                "trail_bumps=COALESCE(?, trail_bumps), "
                "highest_ltp=COALESCE(?, highest_ltp), "
                "lowest_ltp=COALESCE(?, lowest_ltp), "
                "exit_trigger=COALESCE(?, exit_trigger), "
                "initial_sl_price=COALESCE(?, initial_sl_price), "
                "initial_tp_price=COALESCE(?, initial_tp_price) "
                "WHERE trade_id=?",
                (exit_time or _utc_iso(), exit_price, pnl, exit_reason,
                 final_stop_price, trail_bumps, highest_ltp, lowest_ltp,
                 exit_trigger, initial_sl_price, initial_tp_price, trade_id),
            )

    def log_equity_point(
        self,
        current_equity: float,
        peak_equity: float,
        drawdown_pct: float,
        effective_lots: int,
        trading_mode: str = "paper",
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO equity_curve(timestamp, current_equity, "
                "peak_equity, drawdown_pct, effective_lots, trading_mode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_utc_iso(), current_equity, peak_equity, drawdown_pct,
                 effective_lots, trading_mode),
            )

    # ------------------------------------------------------------------ reads
    def latest_equity(self, trading_mode: Optional[str] = None) -> Optional[tuple[float, float]]:
        """Return (peak_equity, current_equity) most recent row.

        If `trading_mode` is supplied, only consider rows logged under that mode.
        Drawdown sizing across PAPER ↔ SIM ↔ LIVE mode flips must NOT see each
        other's equity, so callers pass the active mode.
        """
        with self._cursor() as cur:
            if trading_mode:
                cur.execute(
                    "SELECT peak_equity, current_equity FROM equity_curve "
                    "WHERE trading_mode = ? ORDER BY timestamp DESC LIMIT 1",
                    (trading_mode,),
                )
            else:
                cur.execute(
                    "SELECT peak_equity, current_equity FROM equity_curve "
                    "ORDER BY timestamp DESC LIMIT 1"
                )
            row = cur.fetchone()
        return row if row else None

    def daily_trade_count(self, day_iso_date: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM trades WHERE entry_time LIKE ?",
                (f"{day_iso_date}%",),
            )
            (n,) = cur.fetchone()
        return int(n)

    def consecutive_losses(self) -> int:
        with self._cursor() as cur:
            cur.execute(
                "SELECT pnl FROM trades WHERE pnl IS NOT NULL "
                "ORDER BY entry_time DESC LIMIT 20"
            )
            rows = cur.fetchall()
        count = 0
        for (pnl,) in rows:
            if pnl is None:
                continue
            if pnl < 0:
                count += 1
            else:
                break
        return count

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ───────────────────────────────────────────────────────── commands queue
    def enqueue_command(self, action: str, payload: str = "{}") -> int:
        """Insert a pending command for the bot to pick up. Returns row id."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO commands(timestamp, action, payload, status) "
                "VALUES (?, ?, ?, 'pending')",
                (_utc_iso(), action, payload),
            )
            return int(cur.lastrowid)

    def fetch_pending_command(self) -> Optional[tuple[int, str, str]]:
        """Atomically grab one pending command and mark it 'running'."""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT id, action, payload FROM commands WHERE status='pending' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cmd_id, action, payload = row
            cur.execute(
                "UPDATE commands SET status='running' WHERE id=? AND status='pending'",
                (cmd_id,),
            )
            if cur.rowcount == 0:
                return None  # raced
            return cmd_id, action, payload

    def complete_command(self, cmd_id: int, ok: bool, result: str = "") -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE commands SET status=?, result=? WHERE id=?",
                ("done" if ok else "failed", result, cmd_id),
            )

    # ───────────────────────────────────────────────────── bot live state
    def set_state(self, key: str, value: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO bot_state(key, value, updated) VALUES (?, ?, ?)",
                (key, value, _utc_iso()),
            )

    def get_state(self, key: str) -> Optional[tuple[str, str]]:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT value, updated FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
        return row if row else None

    # ─── v1.14 Phase Y — broker API audit persistence ────────────────
    def record_broker_audit(self, entry: dict) -> None:
        """Persist one broker call for cross-process (/api/bot/broker_audit)
        visibility. Called by BrokerAudit's sink; never raises."""
        try:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO broker_audit_log("
                    "method, request_ts, response_ts, latency_ms, ok, "
                    "error_code, error_message, broker_order_id, "
                    "exchange_order_id, request_summary, response_summary"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.get("method"),
                        float(entry.get("request_ts") or 0.0),
                        float(entry.get("response_ts") or 0.0),
                        int(entry.get("latency_ms") or 0),
                        1 if entry.get("ok") else 0,
                        entry.get("error_code"),
                        entry.get("error_message"),
                        entry.get("broker_order_id"),
                        entry.get("exchange_order_id"),
                        entry.get("request_summary"),
                        entry.get("response_summary"),
                    ),
                )
        except Exception:
            pass  # audit must never impact trading

    # ─── v2.4 — daily P&L + trigger stats ────────────────────────────
    def realized_pnl_today(self, ist_today_iso: str) -> float:
        """v2.4 — sum of `pnl` for trades that CLOSED today (IST date).
        Only realized/closed trades count — open/floating PnL is excluded.
        `ist_today_iso` is a YYYY-MM-DD string (IST date)."""
        try:
            with self._cursor() as cur:
                # exit_time is stored as UTC ISO; convert on the fly to IST
                # date via SQLite's datetime() and compare the substring.
                row = cur.execute(
                    "SELECT COALESCE(SUM(pnl), 0.0) FROM trades "
                    "WHERE exit_time IS NOT NULL "
                    "AND substr(datetime(exit_time, '+330 minutes'), 1, 10) = ?",
                    (ist_today_iso,),
                ).fetchone()
            return float(row[0] or 0.0)
        except Exception:
            return 0.0

    def trigger_stats(self, ist_today_iso: Optional[str] = None) -> list[dict]:
        """v2.4 — group closed trades by `trigger_reason` returning
        {trigger, trades, wins, losses, win_rate_pct, net_pnl}.
        If `ist_today_iso` is provided, restrict to that IST date."""
        sql = (
            "SELECT COALESCE(trigger_reason, 'UNKNOWN') AS trig, "
            "COUNT(*) AS n, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses, "
            "COALESCE(SUM(pnl), 0.0) AS net "
            "FROM trades WHERE exit_time IS NOT NULL"
        )
        params: tuple = ()
        if ist_today_iso:
            sql += (
                " AND substr(datetime(exit_time, '+330 minutes'), 1, 10) = ?"
            )
            params = (ist_today_iso,)
        sql += " GROUP BY trig ORDER BY trig"
        try:
            with self._cursor() as cur:
                rows = cur.execute(sql, params).fetchall()
        except Exception:
            return []
        out: list[dict] = []
        for r in rows:
            trig = r[0] or "UNKNOWN"
            n = int(r[1] or 0)
            wins = int(r[2] or 0)
            losses = int(r[3] or 0)
            net = float(r[4] or 0.0)
            win_rate = round((wins / n) * 100.0, 1) if n else 0.0
            out.append({
                "trigger": trig, "trades": n, "wins": wins, "losses": losses,
                "win_rate_pct": win_rate, "net_pnl": round(net, 2),
            })
        return out


_singleton: Optional[SqliteLogger] = None


def get_logger(db_path: Optional[str] = None) -> SqliteLogger:
    global _singleton
    if _singleton is None:
        from config import DB_PATH  # local import to dodge circulars
        _singleton = SqliteLogger(db_path or DB_PATH)
    return _singleton
