"""
risk/liquidity_gate.py
======================
Pre-entry liquidity sanity checks on the candidate option book.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import config


@dataclass
class LiquidityVerdict:
    ok: bool
    reasons: list[str]


class LiquidityGate:
    def check(
        self,
        bid: float,
        ask: float,
        volume: int,
        open_interest: int,
    ) -> LiquidityVerdict:
        reasons: list[str] = []

        if bid <= 0 or ask <= 0:
            reasons.append("zero_quote")
            return LiquidityVerdict(False, reasons)

        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid else 1.0
        if spread_pct > config.MAX_BID_ASK_SPREAD_PCT:
            reasons.append(f"spread>{config.MAX_BID_ASK_SPREAD_PCT:.3f}({spread_pct:.4f})")
        if volume < config.MINIMUM_VOLUME:
            reasons.append(f"volume<{config.MINIMUM_VOLUME}({volume})")
        if open_interest < config.MINIMUM_OI:
            reasons.append(f"oi<{config.MINIMUM_OI}({open_interest})")

        return LiquidityVerdict(ok=len(reasons) == 0, reasons=reasons)
