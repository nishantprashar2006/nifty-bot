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
        today = datetime.now(timezone.utc).date()
        candidates: list[tuple[datetime, str]] = []
        for r in rows:
            exp = r.get("expiry", "")
            try:
                d = datetime.strptime(exp, "%d%b%Y").date()
            except ValueError:
                continue
            if d >= today:
                candidates.append((datetime.combine(d, datetime.min.time()), exp))
        if not candidates:
            raise RuntimeError("No future Nifty option expiries found in scrip master.")
        candidates.sort()
        return candidates[0][1]

    # ------------------------------------------------------------------ pick
    def select_atm(self, spot_price: float) -> tuple[OptionContract, OptionContract]:
        """Return (CE, PE) ATM contracts for the nearest weekly expiry."""
        rows = self._nifty_options()
        expiry = self._nearest_expiry(rows)
        nearest = [r for r in rows if r.get("expiry") == expiry]

        # round to nearest 50 strike (Nifty convention)
        atm_strike = round(spot_price / 50) * 50

        def find(opt_type: str) -> dict:
            best = None
            best_diff = float("inf")
            for r in nearest:
                # strike comes as paise integer string in scrip master
                try:
                    strike = float(r.get("strike", 0)) / 100
                except (TypeError, ValueError):
                    continue
                sym = r.get("symbol", "")
                if not sym.endswith(opt_type):
                    continue
                diff = abs(strike - atm_strike)
                if diff < best_diff:
                    best_diff = diff
                    best = r
            if best is None:
                raise RuntimeError(f"No ATM {opt_type} candidate found.")
            return best

        ce = find("CE")
        pe = find("PE")

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
