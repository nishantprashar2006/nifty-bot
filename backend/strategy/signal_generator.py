"""
strategy/signal_generator.py
============================
Detects the *initial* 3-minute EMA(9) / EMA(21) crossover and pairs it with the
regime-permitted direction. Emits one signal per fresh crossover.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import Direction
from data.candle_manager import Bar
from data.indicator_engine import IndicatorSnapshot, bars_to_frame, ema


@dataclass
class CrossSignal:
    direction: Direction
    crossover_bar_close: float
    ema_fast: float
    ema_slow: float


class SignalGenerator:
    def __init__(self, ema_fast: int, ema_slow: int) -> None:
        self.ef = ema_fast
        self.es = ema_slow

    def latest_cross(
        self, bars_3m: list[Bar], snap: IndicatorSnapshot
    ) -> Optional[CrossSignal]:
        if len(bars_3m) < self.es + 2:
            return None
        df = bars_to_frame(bars_3m)
        ef = ema(df["close"], self.ef)
        es = ema(df["close"], self.es)
        if len(ef) < 2:
            return None
        prev_diff = ef.iloc[-2] - es.iloc[-2]
        cur_diff = ef.iloc[-1] - es.iloc[-1]
        last_close = float(df["close"].iloc[-1])

        if prev_diff <= 0 and cur_diff > 0:
            return CrossSignal(Direction.LONG, last_close, float(ef.iloc[-1]), float(es.iloc[-1]))
        if prev_diff >= 0 and cur_diff < 0:
            return CrossSignal(Direction.SHORT, last_close, float(ef.iloc[-1]), float(es.iloc[-1]))
        return None
