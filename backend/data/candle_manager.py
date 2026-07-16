"""
data/candle_manager.py
======================
Live data resampler. Consumes raw ticks for a single instrument and emits
closed OHLCV bars at user-defined intervals (3m and 15m here). All state is
in-memory; persistence is the SQLite logger's job, not ours.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


@dataclass
class Bar:
    ts: datetime          # bar OPEN timestamp, tz-aware (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass
class _LiveBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    last_volume_cum: int = 0  # for volume diff (exchange ships cumulative)


class CandleSeries:
    """Single instrument, single interval, append-on-close ring buffer."""

    def __init__(self, interval_min: int, maxlen: int = 500) -> None:
        if interval_min not in (1, 3, 5, 15, 30, 60):
            raise ValueError(f"unsupported interval: {interval_min}")
        self.interval_min = interval_min
        self.bars: deque[Bar] = deque(maxlen=maxlen)
        self._current: Optional[_LiveBar] = None
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Bar], None]] = []

    # ---------------------------------------------------------------- listen
    def on_bar_close(self, cb: Callable[[Bar], None]) -> None:
        self._listeners.append(cb)

    # ---------------------------------------------------------------- helpers
    def _bucket_start(self, ts: datetime) -> datetime:
        """Floor `ts` to its bucket open time (IST-naive math, UTC tz preserved)."""
        minute = (ts.minute // self.interval_min) * self.interval_min
        return ts.replace(minute=minute, second=0, microsecond=0)

    # ---------------------------------------------------------------- ingest
    def ingest_tick(
        self, price: float, volume_cum: int, ts: Optional[datetime] = None
    ) -> Optional[Bar]:
        """Push a tick. Returns the just-closed bar if one rolled over."""
        ts = ts or datetime.now(timezone.utc)
        with self._lock:
            bucket = self._bucket_start(ts)
            closed: Optional[Bar] = None

            if self._current is None:
                self._current = _LiveBar(
                    ts=bucket, open=price, high=price, low=price, close=price,
                    volume=0, last_volume_cum=volume_cum,
                )
                return None

            # roll-over check
            if bucket > self._current.ts:
                closed = Bar(
                    ts=self._current.ts,
                    open=self._current.open,
                    high=self._current.high,
                    low=self._current.low,
                    close=self._current.close,
                    volume=self._current.volume,
                )
                self.bars.append(closed)
                self._current = _LiveBar(
                    ts=bucket, open=price, high=price, low=price, close=price,
                    volume=0, last_volume_cum=volume_cum,
                )
            else:
                self._current.high = max(self._current.high, price)
                self._current.low = min(self._current.low, price)
                self._current.close = price
                dv = max(0, volume_cum - self._current.last_volume_cum)
                self._current.volume += dv
                self._current.last_volume_cum = volume_cum

        if closed is not None:
            for cb in list(self._listeners):
                try:
                    cb(closed)
                except Exception:  # noqa: BLE001
                    pass
        return closed

    # ---------------------------------------------------------------- views
    def closed_bars(self) -> list[Bar]:
        with self._lock:
            return list(self.bars)

    def working_bar(self) -> Optional[Bar]:
        with self._lock:
            if self._current is None:
                return None
            return Bar(
                ts=self._current.ts,
                open=self._current.open,
                high=self._current.high,
                low=self._current.low,
                close=self._current.close,
                volume=self._current.volume,
            )


class CandleManager:
    """Per-token registry of CandleSeries (3m & 15m), thread-safe."""

    def __init__(self) -> None:
        self._series: dict[tuple[str, int], CandleSeries] = {}
        self._lock = threading.Lock()

    def series(self, token: str, interval_min: int) -> CandleSeries:
        with self._lock:
            key = (token, interval_min)
            if key not in self._series:
                self._series[key] = CandleSeries(interval_min)
            return self._series[key]

    def ingest(
        self,
        token: str,
        price: float,
        volume_cum: int = 0,
        ts: Optional[datetime] = None,
    ) -> None:
        # always feed both 3m & 15m series if registered
        for (tok, _), s in list(self._series.items()):
            if tok == token:
                s.ingest_tick(price, volume_cum, ts)

    def seed_history(
        self,
        token: str,
        interval_min: int,
        bars: list[Bar],
    ) -> None:
        s = self.series(token, interval_min)
        with s._lock:
            s.bars.extend(bars[-s.bars.maxlen :])

    def reset_intraday(self) -> None:
        """v2.3 Phase 4 — clear ALL cached bars + in-progress bars across
        every registered (token, interval) series. Used at EOD so the
        next trading day starts from a genuinely empty slate — no
        yesterday-carrying-forward risk. Series registrations are
        preserved so listeners (bar_close callbacks) stay wired."""
        with self._lock:
            for s in self._series.values():
                with s._lock:
                    s.bars.clear()
                    s._current = None
