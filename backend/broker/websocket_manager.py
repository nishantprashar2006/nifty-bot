"""
broker/websocket_manager.py
===========================
Dual-channel socket manager:

  • Ticks            — quote feed for Nifty spot, VIX, and active option contract.
  • Order-status     — exchange-side fill / reject confirmations.

Each channel runs in its own daemon thread with an asynchronous exponential
backoff heartbeat watchdog. A *continuous* feed silence ≥ 30 s instantly raises
`HeartbeatLapse` to be caught by the main FSM and routed into FORCED_EXIT.

In PAPER mode this manager wires up no real sockets; it exposes the same
contract so the strategy code stays broker-agnostic.
"""
from __future__ import annotations

import logging
import queue
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import config

logger = logging.getLogger(__name__)


class HeartbeatLapse(Exception):
    """Raised when WS feed silence exceeds WS_HEARTBEAT_TIMEOUT_SEC."""


@dataclass
class Tick:
    token: str
    ltp: float
    volume: int
    oi: int
    bid: float
    ask: float
    ts: float


@dataclass
class OrderEvent:
    order_id: str
    status: str          # 'complete' | 'rejected' | 'cancelled' | 'open'
    fill_price: float
    avg_price: float
    text: str
    ts: float


class WebSocketManager:
    """Manages tick + order channels with heartbeat watchdog."""

    def __init__(
        self,
        feed_token: str,
        client_id: str,
        jwt: str = "",
        on_tick: Optional[Callable[[Tick], None]] = None,
        on_order: Optional[Callable[[OrderEvent], None]] = None,
        is_shutdown: Optional[Callable[[], bool]] = None,
        heartbeat_armed: Optional[Callable[[], bool]] = None,
        token_subscriptions: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self._feed_token = feed_token
        self._client_id = client_id
        self._jwt = jwt
        self._on_tick = on_tick
        self._on_order = on_order
        self._is_shutdown = is_shutdown or (lambda: False)
        # Heartbeat trips only when this returns True. Default → never (safer:
        # we DON'T want stale-feed alarms when the bot is just idling outside
        # market hours).
        self._heartbeat_armed = heartbeat_armed or (lambda: False)
        self._token_subs = token_subscriptions or []

        self.tick_queue: "queue.Queue[Tick]" = queue.Queue(maxsize=10_000)
        self.order_queue: "queue.Queue[OrderEvent]" = queue.Queue(maxsize=10_000)

        self._stop = threading.Event()
        self._last_tick_ts = time.time()
        self._reconnect_failures = 0
        self._tick_thread: Optional[threading.Thread] = None
        self._order_thread: Optional[threading.Thread] = None
        self._watch_thread: Optional[threading.Thread] = None

        # P0-Q1: hold a reference to the live SDK ws object so we can call
        # its `subscribe()` again when new option strikes are picked mid-day.
        # `None` while the socket is between reconnects.
        self._sdk_ws = None
        self._sub_lock = threading.RLock()

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._stop.clear()
        if not config.USE_LIVE_DATA:
            logger.info("WS in PAPER mode — no live sockets opened.")
            return
        self._tick_thread = threading.Thread(
            target=self._run_tick_channel, name="ws-tick", daemon=True
        )
        self._order_thread = threading.Thread(
            target=self._run_order_channel, name="ws-order", daemon=True
        )
        self._watch_thread = threading.Thread(
            target=self._heartbeat_watchdog, name="ws-watchdog", daemon=True
        )
        self._tick_thread.start()
        self._order_thread.start()
        self._watch_thread.start()

    def stop(self) -> None:
        self._stop.set()

    # --------------------------------------------------------------- ingestion
    def _publish_tick(self, t: Tick) -> None:
        self._last_tick_ts = time.time()
        if self._on_tick:
            try:
                self._on_tick(t)
            except Exception:
                logger.exception("tick callback raised")
        try:
            self.tick_queue.put_nowait(t)
        except queue.Full:
            try:
                _ = self.tick_queue.get_nowait()  # drop oldest
                self.tick_queue.put_nowait(t)
            except queue.Empty:
                pass

    def _publish_order(self, ev: OrderEvent) -> None:
        if self._on_order:
            try:
                self._on_order(ev)
            except Exception:
                logger.exception("order callback raised")
        try:
            self.order_queue.put_nowait(ev)
        except queue.Full:
            pass

    # --------------------------------------------------------------- channels
    def _run_tick_channel(self) -> None:
        """Backoff-reconnect loop for the tick socket."""
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # lazy import

        attempt = 0
        while not self._stop.is_set():
            try:
                ws = SmartWebSocketV2(
                    auth_token=self._jwt,
                    api_key=config.ANGEL_API_KEY,
                    client_code=self._client_id,
                    feed_token=self._feed_token,
                )

                def on_data(*args, **_kw) -> None:
                    # SmartAPI SDK passes (wsapp, message, type, continue_flag);
                    # we only care about the message body which is args[1] (or last).
                    try:
                        msg = args[1] if len(args) >= 2 else (args[0] if args else {})
                        if not isinstance(msg, dict):
                            return
                        buys = msg.get("best_5_buy_data") or []
                        sells = msg.get("best_5_sell_data") or []
                        bid = float((buys[0].get("price", 0) if buys else 0)) / 100
                        ask = float((sells[0].get("price", 0) if sells else 0)) / 100
                        self._publish_tick(
                            Tick(
                                token=str(msg.get("token", "")),
                                ltp=float(msg.get("last_traded_price", 0.0)) / 100,
                                volume=int(msg.get("volume_trade_for_the_day", 0)),
                                oi=int(msg.get("open_interest", 0)),
                                bid=bid,
                                ask=ask,
                                ts=time.time(),
                            )
                        )
                    except Exception:
                        logger.exception("tick parse failed")

                ws.on_data = on_data
                ws.on_error = lambda *a, **kw: logger.warning("ws-tick error: %s", a)
                ws.on_close = lambda *a, **kw: logger.warning("ws-tick closed")

                # Subscribe to instrument tokens after the socket opens
                def on_open(*_a, **_kw) -> None:
                    if not self._token_subs:
                        return
                    try:
                        ws.subscribe(
                            correlation_id=f"nifty-bot-{int(time.time())}",
                            mode=3,                    # snap quote (full depth)
                            token_list=self._token_subs,
                        )
                        logger.info("WS subscribed to %d instrument groups", len(self._token_subs))
                    except Exception:
                        logger.exception("ws subscribe failed")

                ws.on_open = on_open
                # P0-Q1: expose the live SDK object so mid-session
                # resubscribe() calls can push new tokens without a reconnect.
                with self._sub_lock:
                    self._sdk_ws = ws
                try:
                    ws.connect()  # blocking until closed
                finally:
                    with self._sub_lock:
                        self._sdk_ws = None
                attempt = 0
            except Exception as exc:
                self._reconnect_failures += 1
                attempt += 1
                delay = min(
                    config.WS_BACKOFF_CAP_SEC,
                    config.WS_BACKOFF_BASE_SEC * (2 ** min(attempt, 6)),
                ) + random.uniform(0, 1)
                logger.error(
                    "ws-tick crashed (%s), retrying in %.1fs (fail #%d)",
                    exc, delay, self._reconnect_failures,
                )
                if self._reconnect_failures >= config.MAX_WS_RECONNECT_FAILS:
                    logger.critical("WS reconnect failures exhausted — halting tick loop.")
                    return
                time.sleep(delay)

    def _run_order_channel(self) -> None:
        """Order-status socket; pure passthrough into the OrderEvent queue."""
        # The SmartAPI Python SDK ships a separate websocket for order updates.
        # If unavailable we fall back to short-poll order book.
        try:
            from SmartApi.smartWebSocketOrderUpdate import SmartWebSocketOrderUpdate
        except Exception:  # pragma: no cover - SDK shape changes
            logger.warning("Order-update websocket unavailable; using REST polling.")
            self._poll_order_book()
            return

        while not self._stop.is_set():
            try:
                ws = SmartWebSocketOrderUpdate(
                    auth_token=self._jwt,
                    api_key=config.ANGEL_API_KEY,
                    client_code=self._client_id,
                    feed_token=self._feed_token,
                )

                def on_data(*args, **_kw) -> None:
                    try:
                        msg = args[1] if len(args) >= 2 else (args[0] if args else {})
                        if isinstance(msg, str):
                            import json as _json
                            try:
                                msg = _json.loads(msg)
                            except _json.JSONDecodeError:
                                return
                        if not isinstance(msg, dict):
                            return
                        self._publish_order(
                            OrderEvent(
                                order_id=str(msg.get("orderid", "")),
                                status=str(msg.get("status", "")).lower(),
                                fill_price=float(msg.get("price", 0.0) or 0.0),
                                avg_price=float(msg.get("averageprice", 0.0) or 0.0),
                                text=str(msg.get("text", "")),
                                ts=time.time(),
                            )
                        )
                    except Exception:
                        logger.exception("order parse failed")

                ws.on_data = on_data
                ws.connect()
            except Exception as exc:
                logger.error("ws-order crashed (%s); restarting in 5s", exc)
                time.sleep(5)

    def _poll_order_book(self) -> None:
        """Fallback poller; kept intentionally lean."""
        while not self._stop.is_set():
            time.sleep(2)

    # --------------------------------------------------------------- watchdog
    def _heartbeat_watchdog(self) -> None:
        while not self._stop.is_set():
            # Only fire when explicitly armed (e.g. holding a position). Avoids
            # noisy FORCED_EXIT ↔ COOLDOWN loops outside market hours.
            if self._is_shutdown() or not self._heartbeat_armed():
                time.sleep(2)
                continue
            silence = time.time() - self._last_tick_ts
            if silence >= config.WS_HEARTBEAT_TIMEOUT_SEC:
                logger.critical(
                    "Tick feed silence %.1fs ≥ %ds — raising HeartbeatLapse",
                    silence, config.WS_HEARTBEAT_TIMEOUT_SEC,
                )
                # publish a synthetic "panic" order event so the FSM unblocks
                self._publish_order(
                    OrderEvent(
                        order_id="HEARTBEAT_LAPSE",
                        status="heartbeat_lapse",
                        fill_price=0.0,
                        avg_price=0.0,
                        text="watchdog tripped",
                        ts=time.time(),
                    )
                )
                self._last_tick_ts = time.time()  # avoid retrigger storm
            time.sleep(1)

    # --------------------------------------------------------------- helpers
    def force_tick(self, t: Tick) -> None:
        """Test-/paper-mode helper to inject a tick."""
        self._publish_tick(t)

    def force_order(self, ev: OrderEvent) -> None:
        self._publish_order(ev)

    @property
    def reconnect_failures(self) -> int:
        return self._reconnect_failures

    # ------------------------------------------------------------ P0-Q1 API
    def resubscribe(self, token_subscriptions: list[dict[str, Any]]) -> bool:
        """Push a new subscription list to the LIVE websocket without a
        reconnect. Called by the bot whenever `_refresh_atm_contracts()`
        picks a strike whose token is not already subscribed.

        Returns True if the SDK accepted the subscribe call; False if we
        couldn't reach it (socket down, PAPER mode, SDK stub missing). In
        that case the new list is still cached on `_token_subs` so the
        next successful reconnect will pick it up.
        """
        with self._sub_lock:
            self._token_subs = token_subscriptions or []
            sdk = self._sdk_ws
        if sdk is None:
            return False
        try:
            sdk.subscribe(
                correlation_id=f"nifty-bot-resub-{int(time.time())}",
                mode=3,
                token_list=self._token_subs,
            )
            logger.info(
                "WS resubscribed to %d instrument group(s)",
                len(self._token_subs),
            )
            return True
        except Exception:
            logger.exception("ws resubscribe failed")
            return False

    def subscribed_tokens(self) -> list[str]:
        """Flat list of tokens currently on the subscription list. Used by
        the /api/bot/status ws_health block."""
        out: list[str] = []
        with self._sub_lock:
            for group in self._token_subs:
                for t in group.get("tokens", []) or []:
                    out.append(str(t))
        return out

    def health(self) -> dict[str, Any]:
        """P0 diagnostics — everything the dashboard/API needs to reason
        about feed integrity in a single call."""
        now = time.time()
        with self._sub_lock:
            sdk_up = self._sdk_ws is not None
            tokens = self.subscribed_tokens()
        return {
            "connected": bool(sdk_up),
            "last_tick_ts": float(self._last_tick_ts),
            "seconds_since_last_tick": max(0.0, now - self._last_tick_ts),
            "reconnect_failures": int(self._reconnect_failures),
            "subscribed_tokens": tokens,
            "subscribed_count": len(tokens),
        }
