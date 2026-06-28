"""
data/swing_finder.py
====================
Deterministic fractal swing-high / swing-low detector.

A bar at index `i` is a swing high if its high is strictly greater than the
high of `N` bars to the left and `N` bars to the right (default N=2 — the
classic Bill Williams fractal). Swing lows mirror this for the bar's low.

Used by the SMC engine to build market structure (HH/HL, LH/LL, BOS, CHoCH),
locate liquidity pools (EQH/EQL, sweeps), and anchor premium/discount zones.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from data.candle_manager import Bar

Side = Literal["HIGH", "LOW"]


@dataclass(frozen=True)
class Swing:
    idx: int        # index in the source bar list
    price: float    # the swing high or low price
    side: Side      # HIGH | LOW
    ts: object      # bar timestamp


def find_swings(bars: list[Bar], lookback: int = 2) -> list[Swing]:
    """Return all confirmed swings in chronological order.

    `lookback` is the number of bars required on each side. A bar is only
    a confirmed swing once `lookback` bars have closed after it — hence
    swings are emitted with a deterministic lag of `lookback` bars.
    """
    out: list[Swing] = []
    if len(bars) < 2 * lookback + 1:
        return out
    for i in range(lookback, len(bars) - lookback):
        h = bars[i].high
        l = bars[i].low
        left = bars[i - lookback : i]
        right = bars[i + 1 : i + 1 + lookback]
        if all(h > b.high for b in left) and all(h > b.high for b in right):
            out.append(Swing(idx=i, price=h, side="HIGH", ts=bars[i].ts))
        if all(l < b.low for b in left) and all(l < b.low for b in right):
            out.append(Swing(idx=i, price=l, side="LOW", ts=bars[i].ts))
    return out
