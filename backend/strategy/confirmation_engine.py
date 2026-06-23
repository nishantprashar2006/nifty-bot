"""
strategy/confirmation_engine.py
===============================
Validates the follow-through bar after a fresh cross:

  • Close cleanly in trade direction without violating EMA_SLOW
  • RSI alignment        (>55 long / <45 short)
  • Spot relative to VWAP (above for long / below for short)
  • Momentum acceleration: ADX_now > 20 AND ADX_now - ADX_prev > 0.5
"""
from __future__ import annotations

from dataclasses import dataclass

import config
from config import Direction
from data.indicator_engine import IndicatorSnapshot


@dataclass
class ConfirmationResult:
    ok: bool
    reasons: list[str]


class ConfirmationEngine:
    def validate(
        self, direction: Direction, snap: IndicatorSnapshot
    ) -> ConfirmationResult:
        reasons: list[str] = []

        # need a working set of indicators
        for fld in ("ema9", "ema21", "rsi", "adx", "adx_prev", "vwap", "last_close"):
            if getattr(snap, fld) is None:
                reasons.append(f"missing:{fld}")
        if reasons:
            return ConfirmationResult(False, reasons)

        # 1. clean candle close in trade direction without violating slow EMA
        if direction is Direction.LONG:
            if not (snap.last_close > snap.ema21 and snap.ema9 > snap.ema21):
                reasons.append("close<=ema_slow_or_ef_below_es")
        else:
            if not (snap.last_close < snap.ema21 and snap.ema9 < snap.ema21):
                reasons.append("close>=ema_slow_or_ef_above_es")

        # 2. RSI alignment
        if direction is Direction.LONG and snap.rsi <= config.RSI_LONG:
            reasons.append(f"rsi<=rsi_long({snap.rsi:.1f})")
        if direction is Direction.SHORT and snap.rsi >= config.RSI_SHORT:
            reasons.append(f"rsi>=rsi_short({snap.rsi:.1f})")

        # 3. VWAP location
        if direction is Direction.LONG and snap.last_close < snap.vwap:
            reasons.append("close<vwap")
        if direction is Direction.SHORT and snap.last_close > snap.vwap:
            reasons.append("close>vwap")

        # 4. Momentum acceleration
        if snap.adx <= config.ADX_MIN:
            reasons.append(f"adx<={config.ADX_MIN}({snap.adx:.1f})")
        if (snap.adx - snap.adx_prev) <= config.ADX_DELTA_MIN:
            reasons.append(
                f"adx_delta<={config.ADX_DELTA_MIN}({snap.adx - snap.adx_prev:.2f})"
            )

        return ConfirmationResult(ok=len(reasons) == 0, reasons=reasons)
