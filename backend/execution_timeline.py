"""
execution_timeline.py
=====================
Lightweight, isolated observability layer that records every meaningful
execution event for a trade so the UI can render a complete audit
timeline without reading logs.

Design constraints (v1.10 P1):
    • **No trading behaviour changes**. This module is INSERT-only into
      a dedicated `execution_events` table. It does not influence entry,
      exit, protection, trailing, sizing or FSM transitions.
    • **Cheap**. Every call is a single INSERT (< 1 ms). No queries on
      the hot path.
    • **Fail-open**. Any exception during logging is swallowed with a
      warning — the trading loop never crashes because of an event
      logger failure.
    • **Isolated**. The main FSM only needs to know the ONE public
      method `TimelineLogger.log(...)`. Nothing else.

Event schema (SQLite `execution_events`):
    id          INTEGER PRIMARY KEY AUTOINCREMENT
    ts          TEXT  ISO-8601 UTC
    trade_id    TEXT  either the final trade_id (post-fill) or a
                      session_uuid (pre-fill; rewritten on promote)
    event_type  TEXT  short category — e.g. ENTRY_CLICK, ATM_REFRESH
    message     TEXT  human-readable one-liner shown by the UI
    payload     TEXT  JSON blob with structured metadata (nullable)

Reading side lives in `server.py::/api/bot/trade/{tid}/timeline`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("execution_timeline")


# ── event-type constants (used by both writer and reader) ──────────────
class Event:
    # entry funnel
    ENTRY_CLICK       = "ENTRY_CLICK"
    ATM_REFRESH       = "ATM_REFRESH"
    CONTRACT_SELECTED = "CONTRACT_SELECTED"
    REST_LTP          = "REST_LTP"
    ORDER_SUBMIT      = "ORDER_SUBMIT"
    ORDER_ACK         = "ORDER_ACK"
    ENTRY_FILL        = "ENTRY_FILL"
    # protection
    SL_PLACED         = "SL_PLACED"
    TP_PLACED         = "TP_PLACED"
    PROTECTION_VERIFY = "PROTECTION_VERIFY"
    PROTECTION_RETRY  = "PROTECTION_RETRY"
    # trailing
    TRAIL_BUMP        = "TRAIL_BUMP"
    STOP_MODIFIED     = "STOP_MODIFIED"
    # exit
    EXIT_TRIGGER      = "EXIT_TRIGGER"
    EXIT_SUBMIT       = "EXIT_SUBMIT"
    EXIT_FILL         = "EXIT_FILL"
    # safety
    STALE_FEED        = "STALE_FEED"
    FORCED_EXIT       = "FORCED_EXIT"
    # v1.12 — ORDER_PENDING timeout reconciliation
    ORDER_PENDING_RECONCILE_ORDERBOOK = "ORDER_PENDING_RECONCILE_ORDERBOOK"
    ORDER_PENDING_RECONCILE_POSITION  = "ORDER_PENDING_RECONCILE_POSITION"
    # v1.13 — pre-flight margin check + broker rejection surfacing
    PRECHECK_FAILED   = "PRECHECK_FAILED"
    ORDER_REJECTED    = "ORDER_REJECTED"
    # v1.14 — protection-order rejection surfacing + broker delay
    TP_REJECTED       = "TP_REJECTED"
    SL_REJECTED       = "SL_REJECTED"
    TRAIL_REJECTED    = "TRAIL_REJECTED"
    PROTECTION_HEALTH_OK   = "PROTECTION_HEALTH_OK"
    PROTECTION_HEALTH_FAIL = "PROTECTION_HEALTH_FAIL"
    BROKER_DELAY      = "BROKER_DELAY"
    # v1.14 — Phase Y broker API audit + Phase X pending-timeout attestation
    PENDING_TIMEOUT   = "PENDING_TIMEOUT"
    # v1.15 — Auto-trade mode + safety suspension
    AUTO_MODE_CHANGE  = "AUTO_MODE_CHANGE"
    AUTO_ENTRY        = "AUTO_ENTRY"
    AUTO_SUSPENDED    = "AUTO_SUSPENDED"
    # v2.0 — Auto risk-based position sizing observability
    AUTO_SIZING       = "AUTO_SIZING"
    AUTO_ENTRY_CANCELLED = "AUTO_ENTRY_CANCELLED"
    # informational
    NOTE              = "NOTE"


def new_session_id() -> str:
    """Session key used for events that fire BEFORE the trade_id exists
    (click → refresh → contract-select → order-submit → fill). Rewritten
    to the real trade_id inside `TimelineLogger.rekey_session()`."""
    return f"S-{uuid.uuid4().hex[:10]}"


class TimelineLogger:
    """Isolated writer. One instance per bot; wraps a raw sqlite3 conn.

    All methods are safe to call from the bot's main thread — they are
    fire-and-forget: each `log()` returns immediately even if the write
    fails (logged at WARNING).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    # ---------------------------------------------------------- schema
    def _ensure_schema(self) -> None:
        """Idempotent DDL. Safe to call at every boot."""
        try:
            with sqlite3.connect(self._db_path) as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS execution_events (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts         TEXT NOT NULL,
                        trade_id   TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        message    TEXT NOT NULL,
                        payload    TEXT
                    )
                """)
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_trade "
                    "ON execution_events(trade_id, id)"
                )
                c.commit()
        except Exception:
            logger.exception("execution_events schema init failed")

    # ---------------------------------------------------------- write
    def log(
        self,
        trade_id: str,
        event_type: str,
        message: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Single INSERT. Never raises. Never blocks the caller."""
        try:
            with sqlite3.connect(self._db_path, timeout=1.0) as c:
                c.execute(
                    "INSERT INTO execution_events (ts, trade_id, event_type, message, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        trade_id,
                        event_type,
                        message,
                        json.dumps(payload) if payload else None,
                    ),
                )
                c.commit()
        except Exception:
            logger.exception("timeline.log(%s, %s) failed (ignored)", trade_id, event_type)

    def rekey_session(self, session_id: str, trade_id: str) -> None:
        """When the pending order fills and we finally know the real
        trade_id, rename all the pre-fill events written under the
        session_id so the UI sees ONE contiguous timeline."""
        if not session_id or not trade_id or session_id == trade_id:
            return
        try:
            with sqlite3.connect(self._db_path, timeout=1.0) as c:
                c.execute(
                    "UPDATE execution_events SET trade_id=? WHERE trade_id=?",
                    (trade_id, session_id),
                )
                c.commit()
        except Exception:
            logger.exception("timeline.rekey_session(%s → %s) failed", session_id, trade_id)

    # ---------------------------------------------------------- read
    def timeline_for(self, trade_id: str) -> list[dict[str, Any]]:
        """Reader used by the API. Returns events in chronological order."""
        try:
            with sqlite3.connect(self._db_path) as c:
                c.row_factory = sqlite3.Row
                rows = c.execute(
                    "SELECT id, ts, trade_id, event_type, message, payload "
                    "FROM execution_events WHERE trade_id=? ORDER BY id",
                    (trade_id,),
                ).fetchall()
        except Exception:
            logger.exception("timeline_for(%s) failed", trade_id)
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            try:
                item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
            except Exception:
                item["payload"] = {}
            out.append(item)
        return out
