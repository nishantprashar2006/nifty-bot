"""
data/option_selector.py
=======================
Daily Scrip Master parsing → identify ATM CE / PE option instruments on the
nearest weekly Nifty expiry.

The Scrip Master JSON is ~50MB; we cache it to disk per UTC date so the bot
restarts cheaply.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


@dataclass
class OptionContract:
    symbol: str          # e.g. NIFTY07FEB2622000CE
    token: str
    strike: float
    expiry: str          # DDMMMYY (raw scrip master format)
    option_type: str     # CE / PE
    lot_size: int
    exchange: str = "NFO"


class OptionSelector:
    def __init__(self, cache_dir: str = "/app/backend/data_store/scripmaster") -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._scrip: list[dict] = []

    # ------------------------------------------------------------------ load
    def load(self, force: bool = False) -> None:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        cached = self._cache_dir / f"scripmaster_{today}.json"

        if cached.exists() and not force:
            with cached.open("r") as f:
                self._scrip = json.load(f)
            logger.info("Loaded scrip master from cache (%d rows).", len(self._scrip))
            return

        logger.info("Downloading scrip master from %s", config.SCRIP_MASTER_URL)
        resp = requests.get(config.SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        self._scrip = resp.json()
        with cached.open("w") as f:
            json.dump(self._scrip, f)
        logger.info("Scrip master cached (%d rows).", len(self._scrip))

    # ------------------------------------------------------------------ filter
    def _nifty_options(self) -> list[dict]:
        return [
            row for row in self._scrip
            if row.get("exch_seg") == "NFO"
            and row.get("name") == config.NIFTY_SYMBOL
            and row.get("instrumenttype", "").startswith("OPTIDX")
        ]

    def _nearest_expiry(self, rows: list[dict]) -> str:
        """Pick the nearest **weekly Tuesday** Nifty expiry.

        NSE's Nifty weekly expiry runs on Tuesdays (as of the 2026 rework).
        Older code returned the earliest future date in the scrip master,
        which occasionally selected a further-dated monthly contract with
        materially higher premium. This variant strictly prefers Tuesday
        expiries; only if no Tuesday exists in the master (e.g. an NSE
        holiday shifted the weekly) do we fall back to the earliest.
        """
        today = datetime.now(timezone.utc).date()
        weekly: list[tuple[datetime, str]] = []
        any_future: list[tuple[datetime, str]] = []
        for r in rows:
            exp = r.get("expiry", "")
            try:
                d = datetime.strptime(exp, "%d%b%Y").date()
            except ValueError:
                continue
            if d < today:
                continue
            slot = (datetime.combine(d, datetime.min.time()), exp)
            any_future.append(slot)
            if d.weekday() == 1:   # 0=Mon, 1=Tue, 2=Wed, 3=Thu…
                weekly.append(slot)
        candidates = weekly or any_future
        if not candidates:
            raise RuntimeError("No future Nifty option expiries found in scrip master.")
        candidates.sort()
        chosen = candidates[0][1]
        if not weekly:
            logger.warning(
                "No Tuesday expiry in scrip master — falling back to earliest (%s). "
                "This can happen around NSE holidays.", chosen,
            )
        return chosen

    # ------------------------------------------------------------------ pick
    def select_atm(self, spot_price: float) -> tuple[OptionContract, OptionContract]:
        """Return (CE, PE) contracts for the nearest weekly Tuesday expiry.

        Per user preference (see PRD), we pick **Near-OTM** strikes — this
        matches Angel One's "Most Traded" tag which retail flow gravitates
        towards (better liquidity, higher gamma leverage, lower entry cost).

            CE strike = smallest 50-multiple **strictly greater** than spot
            PE strike = largest  50-multiple **strictly less**    than spot

        Both legs are still on the same weekly expiry so the position
        management logic stays symmetric.
        """
        rows = self._nifty_options()
        expiry = self._nearest_expiry(rows)
        nearest = [r for r in rows if r.get("expiry") == expiry]

        import math
        step = config.NIFTY_STRIKE_STEP if hasattr(config, "NIFTY_STRIKE_STEP") else 50
        # Smallest 50-strike strictly > spot  (for CE Near-OTM)
        ce_strike = math.floor(spot_price / step) * step + step
        # Largest  50-strike strictly < spot  (for PE Near-OTM)
        pe_strike = math.ceil(spot_price / step) * step - step
        # Edge cases when spot is EXACTLY on a strike boundary
        if ce_strike - spot_price <= 0:
            ce_strike += step
        if spot_price - pe_strike <= 0:
            pe_strike -= step

        def find(opt_type: str, target_strike: float) -> dict:
            best = None
            best_diff = float("inf")
            for r in nearest:
                try:
                    strike = float(r.get("strike", 0)) / 100
                except (TypeError, ValueError):
                    continue
                sym = r.get("symbol", "")
                if not sym.endswith(opt_type):
                    continue
                diff = abs(strike - target_strike)
                if diff < best_diff:
                    best_diff = diff
                    best = r
            if best is None:
                raise RuntimeError(f"No Near-OTM {opt_type} candidate at {target_strike} found.")
            return best

        ce = find("CE", ce_strike)
        pe = find("PE", pe_strike)

        def to_contract(r: dict, otype: str) -> OptionContract:
            return OptionContract(
                symbol=r["symbol"],
                token=str(r["token"]),
                strike=float(r["strike"]) / 100,
                expiry=r["expiry"],
                option_type=otype,
                lot_size=int(r.get("lotsize", config.LOT_SIZE_NIFTY)),
            )

        return to_contract(ce, "CE"), to_contract(pe, "PE")
