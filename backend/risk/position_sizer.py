"""
risk/position_sizer.py
======================
Drawdown-aware, premium-spike-guarded lot sizer + daily rupee breakers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import config
from database.sqlite_logger import SqliteLogger


@dataclass
class SizingResult:
    effective_lots: int
    qty: int                   # lots × LOT_SIZE_NIFTY
    drawdown_pct: float
    peak_equity: float
    current_equity: float
    daily_loss_cap: float       # negative number (rupees)
    daily_profit_lock: float    # positive
    scale_multiplier: float


class PositionSizer:
    """Stateless calculator; equity-curve table is the source of truth."""

    def __init__(self, db: SqliteLogger) -> None:
        self._db = db

    # ------------------------------------------------------------------ public
    def compute_base_lots(self, capital: float) -> int:
        return max(config.MIN_LOTS, math.floor(capital / config.CAPITAL_PER_LOT))

    def update_equity_and_size(self, current_equity: float) -> SizingResult:
        """Run the full 6-step sequence and persist an equity_curve row."""
        import config as _cfg
        prev = self._db.latest_equity(trading_mode=_cfg.TRADING_MODE)
        prev_peak = prev[0] if prev else current_equity
        peak_equity = max(prev_peak, current_equity)
        drawdown = (
            0.0
            if peak_equity <= 0
            else max(0.0, (peak_equity - current_equity) / peak_equity)
        )

        scale = self._scale_for(drawdown)

        base_lots = self.compute_base_lots(current_equity)
        if drawdown >= 0.30:
            effective_lots = 1  # forced floor
        else:
            effective_lots = max(1, math.floor(base_lots * scale))

        effective_lots = max(config.MIN_LOTS, min(effective_lots, config.MAX_LOTS_DYNAMIC))

        # daily breakers — locked once finalized for the session
        daily_loss_cap = -config.LOSS_PER_LOT * effective_lots
        daily_profit_lock = config.PROFIT_PER_LOT * effective_lots

        self._db.log_equity_point(
            current_equity=current_equity,
            peak_equity=peak_equity,
            drawdown_pct=drawdown,
            effective_lots=effective_lots,
            trading_mode=_cfg.TRADING_MODE,
        )

        return SizingResult(
            effective_lots=effective_lots,
            qty=effective_lots * config.LOT_SIZE_NIFTY,
            drawdown_pct=drawdown,
            peak_equity=peak_equity,
            current_equity=current_equity,
            daily_loss_cap=float(daily_loss_cap),
            daily_profit_lock=float(daily_profit_lock),
            scale_multiplier=scale,
        )

    def premium_spike_guard(
        self, option_premium: float, effective_lots: int, current_equity: float
    ) -> int:
        """Decrement lots until position_value ≤ equity * MAX_POSITION_VALUE_PCT."""
        cap = current_equity * config.MAX_POSITION_VALUE_PCT
        lots = max(1, effective_lots)
        while lots > 1:
            position_value = option_premium * config.LOT_SIZE_NIFTY * lots
            if position_value <= cap:
                break
            lots -= 1
        return lots

    # ------------------------------------------------------------------ misc
    @staticmethod
    def _scale_for(drawdown_pct: float) -> float:
        if drawdown_pct >= 0.30:
            return 0.0  # floor route — handled above
        if drawdown_pct >= 0.20:
            return 0.50
        if drawdown_pct >= 0.10:
            return 0.75
        return 1.0

    # ------------------------------------------------------------------ ATR
    @staticmethod
    def stops_from_atr(option_atr: float) -> tuple[float, float]:
        sl_points = config.ATR_SL_MULT * option_atr
        tp_points = config.ATR_TP_MULT * option_atr
        return sl_points, tp_points
