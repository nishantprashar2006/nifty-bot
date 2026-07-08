"""
risk/pnl_guard.py
=================
Real-time intraday PnL vs. the *locked* daily rupee circuit breakers.

v2 (P0-7/P0-8): daily PROFIT LOCK removed by user request. Only the daily
LOSS CAP remains — the bot now trades until it hits the loss cap, the
daily trade count, or the user stops it. There is no upside shutdown.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class GuardVerdict:
    breached: bool
    reason: str = ""


class PnlGuard:
    def __init__(self, daily_loss_cap: float) -> None:
        # daily_loss_cap is a NEGATIVE rupee figure (e.g. -750 * lots)
        self._loss_cap = float(daily_loss_cap)
        self._realized = 0.0
        self._lock = threading.Lock()

    # ----------------------------------------------------------- realized
    def add_realized(self, pnl: float) -> None:
        with self._lock:
            self._realized += float(pnl)

    @property
    def realized_pnl(self) -> float:
        with self._lock:
            return self._realized

    @property
    def loss_cap(self) -> float:
        return self._loss_cap

    # ----------------------------------------------------------- guard
    def evaluate(self, unrealized: float = 0.0) -> GuardVerdict:
        with self._lock:
            total = self._realized + unrealized
            if total <= self._loss_cap:
                return GuardVerdict(True, f"daily_loss_cap_hit({total:.2f}≤{self._loss_cap:.2f})")
            return GuardVerdict(False)
