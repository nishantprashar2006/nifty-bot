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
            effective_lots  INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_curve(timestamp)",
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
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO trades(trade_id, entry_time, direction, qty, "
                "entry_price) VALUES (?, ?, ?, ?, ?)",
                (trade_id, entry_time or _utc_iso(), direction, qty, entry_price),
            )

    def update_trade_exit(
        self,
        trade_id: str,
        exit_price: float,
        pnl: float,
        exit_reason: str,
        exit_time: Optional[str] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE trades SET exit_time=?, exit_price=?, pnl=?, exit_reason=? "
                "WHERE trade_id=?",
                (exit_time or _utc_iso(), exit_price, pnl, exit_reason, trade_id),
            )

    def log_equity_point(
        self,
        current_equity: float,
        peak_equity: float,
        drawdown_pct: float,
        effective_lots: int,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO equity_curve(timestamp, current_equity, "
                "peak_equity, drawdown_pct, effective_lots) VALUES (?, ?, ?, ?, ?)",
                (_utc_iso(), current_equity, peak_equity, drawdown_pct, effective_lots),
            )

    # ------------------------------------------------------------------ reads
    def latest_equity(self) -> Optional[tuple[float, float]]:
        """Return (peak_equity, current_equity) most recent row, or None."""
        with self._cursor() as cur:
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


_singleton: Optional[SqliteLogger] = None


def get_logger(db_path: Optional[str] = None) -> SqliteLogger:
    global _singleton
    if _singleton is None:
        from config import DB_PATH  # local import to dodge circulars
        _singleton = SqliteLogger(db_path or DB_PATH)
    return _singleton
