"""
strategy/regime_filter.py
=========================
Higher-timeframe trend context using 15m EMAs on Nifty spot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import Direction
from data.indicator_engine import IndicatorSnapshot


@dataclass
class RegimeVerdict:
    authorize_long: bool
    authorize_short: bool

    @property
    def authorized_direction(self) -> Optional[Direction]:
        if self.authorize_long and not self.authorize_short:
            return Direction.LONG
        if self.authorize_short and not self.authorize_long:
            return Direction.SHORT
        return None


class RegimeFilter:
    """`Fast > Slow` ⇒ Long only · `Fast < Slow` ⇒ Short only · else neutral."""

    def evaluate(self, snap: IndicatorSnapshot) -> RegimeVerdict:
        f, s = snap.ema20_15m, snap.ema50_15m
        if f is None or s is None:
            return RegimeVerdict(False, False)
        return RegimeVerdict(
            authorize_long=f > s,
            authorize_short=f < s,
        )
