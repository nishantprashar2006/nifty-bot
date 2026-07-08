"""
strategy/position_manager.py
============================
Local OCO bookkeeper. Tracks:

  • Open position (single, locked)
  • Pending entry order + 20s timer
  • Resting target & stop-loss legs (one-cancels-other locally)
  • Directional cooldown after a stop-out (15 min) per Direction

The exchange does its own OCO via STOPLOSS_LIMIT + Limit; we just mirror it so
we can issue cancel-the-other-leg payloads on partial/full fills.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from config import Direction


@dataclass
class OpenPosition:
    trade_id: str
    direction: Direction
    contract_symbol: str
    contract_token: str
    qty: int                 # total quantity (lots × lot_size)
    lots: int
    entry_price: float
    entry_ts: datetime
    target_price: float
    stop_price: float
    # P0-4: full contract identity persisted with every trade
    strike: float = 0.0
    expiry: str = ""
    option_type: str = ""    # "CE" | "PE"
    lot_size: int = 0
    target_order_id: Optional[str] = None
    stop_order_id: Optional[str] = None
    trail_anchor: float = 0.0    # last premium high used to trail SL up
    # PART 3 manual-mode trailing — when > 0, premium percent step replaces
    # the global TRAILING_TRIGGER_STEP for this specific position.
    trail_step_pct: float = 0.0

    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.entry_ts).total_seconds()


@dataclass
class PendingEntry:
    order_id: str
    direction: Direction
    contract_symbol: str
    contract_token: str
    expected_price: float
    lots: int
    qty: int
    target_price: float
    stop_price: float
    # P0-4: full contract identity carried through the pending phase so it
    # can be persisted verbatim on fill, without re-reading self._ce/_pe.
    strike: float = 0.0
    expiry: str = ""
    option_type: str = ""
    lot_size: int = 0
    sl_points: float = 0.0     # ATR-derived; used to re-anchor on actual fill
    tp_points: float = 0.0
    # PART 3 — when > 0 these supersede sl/tp_points and recompute the legs
    # from the ACTUAL fill price as a percentage of premium (manual mode).
    sl_pct: float = 0.0
    tp_pct: float = 0.0
    trail_step_pct: float = 0.0
    placed_ts: float = field(default_factory=time.time)

    def age_seconds(self) -> float:
        return time.time() - self.placed_ts


class PositionManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open: Optional[OpenPosition] = None
        self._pending: Optional[PendingEntry] = None
        self._cooldown_until: dict[Direction, datetime] = {}

    # ------------------------------------------------------------- generic
    @property
    def has_open_position(self) -> bool:
        with self._lock:
            return self._open is not None

    @property
    def has_pending_entry(self) -> bool:
        with self._lock:
            return self._pending is not None

    @property
    def open_position(self) -> Optional[OpenPosition]:
        with self._lock:
            return self._open

    @property
    def pending_entry(self) -> Optional[PendingEntry]:
        with self._lock:
            return self._pending

    # ------------------------------------------------------------- entry flow
    def register_pending_entry(self, p: PendingEntry) -> None:
        with self._lock:
            if self._open is not None or self._pending is not None:
                raise RuntimeError("Single-position lock violated.")
            self._pending = p

    def adopt_open_position(self, pos: OpenPosition) -> None:
        """Crash-recovery: re-attach to a broker-side position that survived
        a bot restart. Bypasses the single-position lock since the lock is
        what we're restoring."""
        with self._lock:
            self._open = pos
            self._pending = None

    def clear_pending(self) -> None:
        with self._lock:
            self._pending = None

    def promote_to_open(self, fill_price: float) -> OpenPosition:
        with self._lock:
            if self._pending is None:
                raise RuntimeError("No pending entry to promote.")
            p = self._pending
            # Re-anchor SL/TP to ACTUAL fill price. Manual mode uses % of
            # premium; auto path uses ATR-based points.
            if p.sl_pct > 0 and p.tp_pct > 0:
                target_price = fill_price * (1 + p.tp_pct)
                stop_price = max(0.05, fill_price * (1 - p.sl_pct))
            elif p.sl_points > 0 and p.tp_points > 0:
                target_price = fill_price + p.tp_points
                stop_price = max(0.05, fill_price - p.sl_points)
            else:
                target_price = p.target_price
                stop_price = p.stop_price
            pos = OpenPosition(
                trade_id=f"T-{uuid.uuid4().hex[:10]}",
                direction=p.direction,
                contract_symbol=p.contract_symbol,
                contract_token=p.contract_token,
                qty=p.qty,
                lots=p.lots,
                entry_price=fill_price,
                entry_ts=datetime.now(timezone.utc),
                target_price=target_price,
                stop_price=stop_price,
                # P0-4: carry the full contract identity from pending → open
                strike=p.strike,
                expiry=p.expiry,
                option_type=p.option_type,
                lot_size=p.lot_size,
                trail_anchor=fill_price,
                trail_step_pct=p.trail_step_pct,
            )
            self._open = pos
            self._pending = None
            return pos

    # ------------------------------------------------------------- exit flow
    def set_protective_orders(self, target_id: str, stop_id: str) -> None:
        with self._lock:
            if self._open is None:
                return
            self._open.target_order_id = target_id
            self._open.stop_order_id = stop_id

    def close_position(self, exit_was_stop: bool) -> Optional[OpenPosition]:
        """Tear down the open position, returning it for logging."""
        with self._lock:
            pos = self._open
            self._open = None
            if pos is not None and exit_was_stop:
                until = datetime.now(timezone.utc) + timedelta(
                    minutes=config.REENTRY_BLOCK_MIN
                )
                self._cooldown_until[pos.direction] = until
            return pos

    # ------------------------------------------------------------- trailing
    def maybe_trail_stop(self, current_premium: float) -> Optional[float]:
        """If premium advanced ≥ trailing step since trail_anchor, bump the
        stop by that step. Returns new stop or None.

        Manual-mode positions use a PERCENT step (`trail_step_pct` × entry
        price). Auto-mode positions keep the legacy fixed-points step
        (config.TRAILING_TRIGGER_STEP) so the original behaviour is preserved.
        """
        with self._lock:
            pos = self._open
            if pos is None:
                return None
            if pos.trail_step_pct > 0:
                step = max(0.05, pos.entry_price * pos.trail_step_pct)
            else:
                step = config.TRAILING_TRIGGER_STEP
            delta = current_premium - pos.trail_anchor
            if delta >= step:
                bump = step * (delta // step)
                pos.stop_price += bump
                pos.trail_anchor += bump
                return pos.stop_price
            return None

    # ------------------------------------------------------------- cooldown
    def in_cooldown(self, direction: Direction) -> bool:
        with self._lock:
            until = self._cooldown_until.get(direction)
            if until is None:
                return False
            if datetime.now(timezone.utc) >= until:
                self._cooldown_until.pop(direction, None)
                return False
            return True
