"""
data/indicator_engine.py
========================
Pandas-driven indicator suite. We compute EMAs, RSI(14), ADX(14), VWAP, and
ATR(14) on a `list[Bar]` slice. We deliberately implement these natively
rather than depending on `pandas_ta` (unavailable for Python 3.11 on PyPI as of
Feb-2026). Behaviour matches the canonical Wilder formulas.

Also exposes a tiny `VixTracker` that latches the latest India VIX print so the
IDLE gate can check `11 ≤ VIX ≤ 22`.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from data.candle_manager import Bar


# ──────────────────────────────────────────────────────────────────────
# Indicator primitives (pure functions, vectorised on pd.Series)
# ──────────────────────────────────────────────────────────────────────
def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder smoothing == RMA == EMA with alpha = 1/length
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Classic Wilder ADX. Returns ADX only (DI+/DI- intermediate)."""
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = pd.concat(
        [(high - low),
         (high - close.shift()).abs(),
         (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr_ = tr.ewm(alpha=1 / length, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / length, adjust=False).mean().fillna(0.0)


def vwap(bars: list[Bar]) -> float:
    """Session VWAP using typical price weighted by bar volume."""
    if not bars:
        return float("nan")
    tp = np.array([(b.high + b.low + b.close) / 3 for b in bars], dtype=float)
    vol = np.array([max(b.volume, 1) for b in bars], dtype=float)
    return float((tp * vol).sum() / vol.sum())


# ──────────────────────────────────────────────────────────────────────
# Dataframe builder
# ──────────────────────────────────────────────────────────────────────
def bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        },
        index=pd.DatetimeIndex([b.ts for b in bars], name="ts"),
    )
    return df


# ──────────────────────────────────────────────────────────────────────
# India VIX live latch
# ──────────────────────────────────────────────────────────────────────
class VixTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[float] = None

    def update(self, v: float) -> None:
        with self._lock:
            self._value = float(v)

    @property
    def value(self) -> Optional[float]:
        with self._lock:
            return self._value


# ──────────────────────────────────────────────────────────────────────
# Snapshot container
# ──────────────────────────────────────────────────────────────────────
@dataclass
class IndicatorSnapshot:
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    ema20_15m: Optional[float] = None
    ema50_15m: Optional[float] = None
    rsi: Optional[float] = None
    adx: Optional[float] = None
    adx_prev: Optional[float] = None
    vwap: Optional[float] = None
    atr_3m: Optional[float] = None
    last_close: Optional[float] = None
    macro_last_close: Optional[float] = None


class IndicatorEngine:
    """Computes a snapshot bundle on demand from current candle buffers."""

    def __init__(
        self,
        ema_fast: int,
        ema_slow: int,
        ema_macro_fast: int,
        ema_macro_slow: int,
        rsi_period: int,
        atr_period: int,
    ) -> None:
        self.ef = ema_fast
        self.es = ema_slow
        self.mf = ema_macro_fast
        self.ms = ema_macro_slow
        self.rp = rsi_period
        self.atrp = atr_period

    # ------------------------------------------------------------------ build
    def build_snapshot(
        self,
        bars_3m: list[Bar],
        bars_15m: list[Bar],
        option_bars_3m: Optional[list[Bar]] = None,
    ) -> IndicatorSnapshot:
        snap = IndicatorSnapshot()

        if len(bars_3m) >= max(self.es, self.rp) + 2:
            df = bars_to_frame(bars_3m)
            snap.ema9 = float(ema(df["close"], self.ef).iloc[-1])
            snap.ema21 = float(ema(df["close"], self.es).iloc[-1])
            snap.rsi = float(rsi(df["close"], self.rp).iloc[-1])
            adx_series = adx(df["high"], df["low"], df["close"], 14)
            snap.adx = float(adx_series.iloc[-1])
            snap.adx_prev = float(adx_series.iloc[-2]) if len(adx_series) >= 2 else None
            snap.vwap = vwap(bars_3m)
            snap.last_close = float(df["close"].iloc[-1])

        if len(bars_15m) >= self.ms + 1:
            df15 = bars_to_frame(bars_15m)
            snap.ema20_15m = float(ema(df15["close"], self.mf).iloc[-1])
            snap.ema50_15m = float(ema(df15["close"], self.ms).iloc[-1])
            snap.macro_last_close = float(df15["close"].iloc[-1])

        if option_bars_3m and len(option_bars_3m) >= self.atrp + 1:
            ob = bars_to_frame(option_bars_3m)
            snap.atr_3m = float(atr(ob["high"], ob["low"], ob["close"], self.atrp).iloc[-1])

        return snap
