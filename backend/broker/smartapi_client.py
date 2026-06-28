"""
broker/smartapi_client.py
=========================
Handles Angel One SmartAPI REST session creation, pyotp automated morning
credentialing, and ongoing token validation. Wraps a thin facade so the rest
of the bot does not import the SDK directly.

Falls back to a deterministic *paper* client when PAPER_MODE=True so the
strategy can be exercised without firing real exchange orders.
"""
from __future__ import annotations

import logging
import threading
import time as _time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pyotp

import config

logger = logging.getLogger(__name__)


class SmartApiError(Exception):
    """Raised on broker-side rejections / auth faults."""


# ──────────────────────────────────────────────────────────────────────
# LIVE client
# ──────────────────────────────────────────────────────────────────────
class _LiveSmartApiClient:
    """Thin wrapper around smartapi.SmartConnect with auto-login + heartbeat."""

    def __init__(self) -> None:
        from SmartApi import SmartConnect  # lazy import

        self._api = SmartConnect(api_key=config.ANGEL_API_KEY)
        self._jwt: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._refresh: Optional[str] = None
        self._last_validation_ts: float = 0.0
        self._lock = threading.RLock()

    # ----------------------------------------------------------- session
    def login(self) -> dict[str, Any]:
        """Generate session with TOTP-based 2FA."""
        with self._lock:
            totp = pyotp.TOTP(config.ANGEL_TOTP_KEY).now()
            data = self._api.generateSession(
                config.ANGEL_CLIENT_ID, config.ANGEL_PIN, totp
            )
            if not data or not data.get("status"):
                raise SmartApiError(f"Login failed: {data}")
            tokens = data["data"]
            self._jwt = tokens["jwtToken"]
            self._refresh = tokens["refreshToken"]
            self._feed_token = self._api.getfeedToken()
            self._last_validation_ts = _time.time()
            logger.info("SmartAPI session established for %s", config.ANGEL_CLIENT_ID)
            return tokens

    def validate_session(self, max_age_sec: int = 300) -> None:
        """Ping profile to confirm jwt still valid; relog on failure."""
        with self._lock:
            now = _time.time()
            if now - self._last_validation_ts < max_age_sec:
                return
            try:
                prof = self._api.getProfile(self._refresh)
                if not prof or not prof.get("status"):
                    raise SmartApiError("Profile probe failed")
                self._last_validation_ts = now
            except Exception as exc:  # broad catch: SDK leaks heterogeneous errors
                logger.warning("Session validation failed (%s); re-logging in.", exc)
                self.login()

    # ----------------------------------------------------------- account
    def get_net_available_cash(self) -> float:
        with self._lock:
            funds = self._api.rmsLimit()
            if not funds or not funds.get("status"):
                raise SmartApiError(f"RMS funds query failed: {funds}")
            return float(funds["data"]["net"])

    def get_feed_token(self) -> str:
        return self._feed_token or ""

    def get_jwt(self) -> str:
        return self._jwt or ""

    # ----------------------------------------------------------- orders
    def place_order(self, payload: dict[str, Any]) -> str:
        with self._lock:
            resp = self._api.placeOrder(payload)
            # Angel returns dict {success: False, message, errorCode} on errors;
            # the smartapi-python SDK ALSO logs and returns it instead of raising.
            if isinstance(resp, dict):
                if not resp.get("status", True) or resp.get("success") is False:
                    msg = resp.get("message") or resp.get("errorMessage") or str(resp)
                    raise SmartApiError(f"placeOrder rejected: {msg}")
                data = resp.get("data") or {}
                oid = data.get("orderid") if isinstance(data, dict) else None
                if not oid:
                    raise SmartApiError(f"placeOrder returned no orderid: {resp}")
                return str(oid)
            if not resp or resp == "None":
                raise SmartApiError("placeOrder returned empty response")
            return str(resp)

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> None:
        with self._lock:
            resp = self._api.cancelOrder(order_id, variety)
            if isinstance(resp, dict) and (
                not resp.get("status", True) or resp.get("success") is False
            ):
                msg = resp.get("message") or str(resp)
                raise SmartApiError(f"cancelOrder rejected: {msg}")

    def order_book(self) -> list[dict[str, Any]]:
        with self._lock:
            resp = self._api.orderBook()
            return resp.get("data", []) if isinstance(resp, dict) else []

    def positions(self) -> list[dict[str, Any]]:
        """Current open positions (net). Used for crash-recovery — if the
        bot restarts while a position is open, we adopt it back into the
        FSM instead of orphaning it on the broker."""
        with self._lock:
            try:
                resp = self._api.position()
            except Exception as exc:
                raise SmartApiError(f"position() failed: {exc}") from exc
            if not isinstance(resp, dict):
                return []
            return resp.get("data") or []

    def ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> float:
        with self._lock:
            resp = self._api.ltpData(exchange, tradingsymbol, symboltoken)
            if not resp or not resp.get("status"):
                raise SmartApiError(f"ltpData failed: {resp}")
            return float(resp["data"]["ltp"])

    def logout(self) -> None:
        with self._lock:
            try:
                self._api.terminateSession(config.ANGEL_CLIENT_ID)
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────
# PAPER client (default in this environment)
# ──────────────────────────────────────────────────────────────────────
class _PaperSmartApiClient:
    """Deterministic in-memory broker for dry-run testing of the FSM."""

    def __init__(self, starting_cash: float | None = None) -> None:
        import os as _os
        if starting_cash is None:
            try:
                starting_cash = float(_os.environ.get("PAPER_STARTING_CAPITAL", "200000"))
            except ValueError:
                starting_cash = 200_000.0
        self._cash = starting_cash
        self._orders: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        logger.warning("PAPER mode active — no live orders will be transmitted.")

    def login(self) -> dict[str, Any]:
        return {"jwtToken": "PAPER", "refreshToken": "PAPER", "feedToken": "PAPER"}

    def validate_session(self, max_age_sec: int = 300) -> None:
        return None

    def get_net_available_cash(self) -> float:
        return self._cash

    def get_feed_token(self) -> str:
        return "PAPER"

    def get_jwt(self) -> str:
        return "PAPER"

    def place_order(self, payload: dict[str, Any]) -> str:
        with self._lock:
            oid = f"PAPER-{uuid.uuid4().hex[:10]}"
            self._orders[oid] = {
                **payload,
                "status": "complete",
                "fill_price": float(payload.get("price") or 0.0),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            return oid

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> None:
        with self._lock:
            if order_id in self._orders:
                self._orders[order_id]["status"] = "cancelled"

    def order_book(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"orderid": k, **v} for k, v in self._orders.items()]

    def positions(self) -> list[dict[str, Any]]:
        """Paper broker has no carry — always flat at boot."""
        return []

    def ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> float:
        return 0.0

    def logout(self) -> None:
        return None


# ──────────────────────────────────────────────────────────────────────
# HYBRID client — real Angel data + cash, simulated order fills (SIM mode)
# ──────────────────────────────────────────────────────────────────────
class _HybridSmartApiClient(_LiveSmartApiClient):
    """Live REST/WS for market data + RMS cash; orders are paper-simulated."""

    def __init__(self) -> None:
        super().__init__()
        self._sim_orders: dict[str, Any] = {}
        logger.warning("SIM mode active — real ticks, but orders are simulated.")

    def place_order(self, payload: dict[str, Any]) -> str:  # type: ignore[override]
        oid = f"SIM-{uuid.uuid4().hex[:10]}"
        self._sim_orders[oid] = {
            **payload,
            "status": "complete",
            "fill_price": float(payload.get("price") or 0.0),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        return oid

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> None:  # type: ignore[override]
        if order_id in self._sim_orders:
            self._sim_orders[order_id]["status"] = "cancelled"

    def order_book(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return [{"orderid": k, **v} for k, v in self._sim_orders.items()]

    def positions(self) -> list[dict[str, Any]]:  # type: ignore[override]
        """SIM mode is always flat at boot — no real positions to adopt."""
        return []


# ──────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────
def _select_client_cls():
    if config.TRADING_MODE == "live":
        return _LiveSmartApiClient
    # default for "sim" (or any unrecognized value)
    return _HybridSmartApiClient


SmartApiClient = _select_client_cls()


def build_client() -> Any:
    client = _select_client_cls()()
    client.login()
    return client
