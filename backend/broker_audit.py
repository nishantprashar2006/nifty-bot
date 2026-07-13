"""
broker_audit.py — Phase Y diagnostic layer (v1.14).

Wraps the live broker client so every call to `placeOrder / modifyOrder /
cancelOrder / order_book / positions` is recorded into a bounded
in-memory ring. Zero effect on trading behaviour: return values,
exceptions, and timings are passed through untouched — the wrapper only
observes.

Consumed by:
  • `/api/bot/broker_audit` — UI panel + diagnostic download.
  • `main.py::_step_order_pending` — the ring's last few entries are
    surfaced inside the PENDING_TIMEOUT timeline attestation.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Optional

# Only these five methods are audited — everything else on the broker
# client is passed through unchanged.
_AUDITED_METHODS = ("place_order", "modify_order", "cancel_order",
                    "order_book", "positions")


def _summarise(obj: Any, max_len: int = 800) -> str:
    """Human-readable one-liner for the UI. Full payload also kept raw."""
    try:
        s = repr(obj)
    except Exception:
        return f"<un-reprable {type(obj).__name__}>"
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


class BrokerAudit:
    """Bounded ring of the most recent broker interactions."""

    def __init__(self, capacity: int = 100, sink: Optional[Callable[[dict], None]] = None) -> None:
        self._ring: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._seq = 0
        self._sink = sink

    # ─── recording ─────────────────────────────────────────────
    def _record(
        self,
        method: str,
        args: tuple,
        kwargs: dict,
        t0: float,
        t1: float,
        response: Any,
        error: Optional[BaseException],
    ) -> None:
        entry = {
            "seq": self._next_seq(),
            "method": method,
            "request_ts": t0,
            "response_ts": t1,
            "latency_ms": int(max(0.0, (t1 - t0) * 1000)),
            "ok": error is None,
            "status": "ok" if error is None else "error",
            "error_code": None,
            "error_message": None if error is None else str(error),
            "broker_order_id": None,
            "exchange_order_id": None,
            "request_summary": _summarise({"args": args, "kwargs": kwargs}),
            "response_summary": None if error is not None else _summarise(response),
            "raw_response": response if error is None else None,
        }
        # Best-effort field extraction — never let it break the recorder.
        try:
            if isinstance(response, dict):
                data = response.get("data") or {}
                if isinstance(data, dict):
                    entry["broker_order_id"] = str(data.get("orderid") or "") or None
                    entry["exchange_order_id"] = str(
                        data.get("uniqueorderid") or data.get("exchange_order_id") or ""
                    ) or None
            elif isinstance(response, str) and method == "place_order":
                entry["broker_order_id"] = response
            if isinstance(response, dict) and response.get("errorcode"):
                entry["error_code"] = str(response.get("errorcode"))
        except Exception:
            pass
        with self._lock:
            self._ring.append(entry)
        # Persistent sink for cross-process visibility (server.py process
        # reads from SQLite). Never raise from here — audit must never
        # affect trading behaviour.
        if self._sink is not None:
            try:
                self._sink(entry)
            except Exception:
                pass

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    # ─── read ──────────────────────────────────────────────────
    def snapshot(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return a shallow copy suitable for JSON serialization."""
        with self._lock:
            items = list(self._ring)[-limit:]
        out = []
        for e in items:
            row = dict(e)
            row.pop("raw_response", None)  # keep API-safe by default
            out.append(row)
        return out

    def recent_by_method(self, method: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            hits = [e for e in self._ring if e["method"] == method][-limit:]
        out = []
        for e in hits:
            row = dict(e)
            row.pop("raw_response", None)
            out.append(row)
        return out

    # ─── wrap ──────────────────────────────────────────────────
    def wrap(self, broker: Any) -> "AuditedBroker":
        return AuditedBroker(broker, self)


class AuditedBroker:
    """Transparent proxy. Recorded methods get instrumented; anything else
    passes through unchanged. Raised exceptions are re-raised (never swallowed)."""

    def __init__(self, inner: Any, audit: BrokerAudit) -> None:
        # Store on __dict__ to avoid __getattr__ recursion.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_audit", audit)

    def __getattr__(self, name: str) -> Any:
        # Delegate everything not on the proxy itself.
        target = getattr(self._inner, name)
        if name not in _AUDITED_METHODS or not callable(target):
            return target
        audit = self._audit
        method_name = name

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            t0 = time.time()
            try:
                resp = target(*args, **kwargs)
                t1 = time.time()
                audit._record(method_name, args, kwargs, t0, t1, resp, None)
                return resp
            except BaseException as exc:
                t1 = time.time()
                audit._record(method_name, args, kwargs, t0, t1, None, exc)
                raise
        wrapped.__name__ = f"audited_{name}"
        return wrapped
