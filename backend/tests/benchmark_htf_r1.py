"""
Benchmark harness for the R1 refinement to `detect_structure`.

Compares OLD strict-`>`/`<` vs NEW EQ-tolerance detector on synthetic
15m sessions that produce realistic swings (multi-bar pullbacks) —
otherwise `find_swings(lookback=5)` returns nothing and both detectors
degenerate to NEUTRAL by starvation.

Success criteria (per user):
  1. False-NEUTRAL rate must drop for trending scenarios.
  2. Sideways / choppy must NOT flip to CALL or PUT.

Run:
    cd /app/backend && python -m tests.benchmark_htf_r1
"""
from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.candle_manager import Bar  # noqa: E402
from data.swing_finder import find_swings  # noqa: E402
from strategy.smc_engine import SWING_WINDOW, detect_structure as NEW_detect_structure  # noqa: E402


# ─────────────────────────────────────────────────────────────────
# OLD detector — verbatim pre-R1 logic.
# ─────────────────────────────────────────────────────────────────
def OLD_detect_structure(swings):
    highs = [s for s in swings if s.side == "HIGH"]
    lows = [s for s in swings if s.side == "LOW"]

    if len(highs) >= 3 and len(lows) >= 3:
        hh = highs[-1].price > highs[-3].price
        hl = lows[-1].price > lows[-3].price
        lh = highs[-1].price < highs[-3].price
        ll = lows[-1].price < lows[-3].price
    elif len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        lh = highs[-1].price < highs[-2].price
        ll = lows[-1].price < lows[-2].price
    else:
        return "NEUTRAL"

    if hh and hl:
        return "CALL"
    if lh and ll:
        return "PUT"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────
# Bar-series generator that produces confirmable 5-fractal swings.
#
# We build the price path as a sequence of "legs" — each leg is a
# straight move of `leg_len` bars in a given direction. The turn
# between two opposite legs creates a swing that `find_swings` can
# confirm once ≥5 bars have printed after it.
# ─────────────────────────────────────────────────────────────────
_T0 = datetime(2026, 2, 17, 9, 15, tzinfo=timezone.utc)


def _mk_bar(i: int, o: float, h: float, l: float, c: float) -> Bar:
    ts = _T0 + timedelta(minutes=i * 15)
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=1000)


def _walk(legs, rng, wick_min=3, wick_max=8):
    """`legs` is a list of (direction, bars_in_leg, avg_move_per_bar).
    Builds a bar list; the last bar of each leg is a swing candidate.
    """
    bars = []
    price = 24000.0
    i = 0
    for direction, n, move in legs:
        for _ in range(n):
            step = move * direction * rng.uniform(0.7, 1.3)
            o = price
            c = price + step
            wick_up = rng.uniform(wick_min, wick_max)
            wick_dn = rng.uniform(wick_min, wick_max)
            h = max(o, c) + wick_up
            l = min(o, c) - wick_dn
            bars.append(_mk_bar(i, o, h, l, c))
            price = c
            i += 1
    return bars


def _gen_uptrend(rng: random.Random) -> list[Bar]:
    """Bullish 15m: up-leg 8 bars → pullback 6 → up-leg 8 → pullback 6
    → up-leg 8. Produces multiple confirmable HH/HL swings."""
    return _walk(
        [(+1, 8, 30), (-1, 6, 15), (+1, 8, 30), (-1, 6, 15), (+1, 8, 30)],
        rng,
    )


def _gen_uptrend_equal_lows(rng: random.Random) -> list[Bar]:
    """Same shape as uptrend but engineered so the two pullback troughs
    finish at ≈ equal price (within 5 bps at NIFTY scale). This is the
    Scenario A dominant intraday miss that R1 targets."""
    bars = []
    price = 24000.0
    i = 0
    # up leg 1: 8 bars +30/bar → ends ~24240
    for _ in range(8):
        o, c = price, price + 30 * rng.uniform(0.9, 1.1)
        bars.append(_mk_bar(i, o, max(o, c) + 6, min(o, c) - 4, c))
        price = c
        i += 1
    peak1 = price
    # pullback 1: 6 bars -25/bar → ends ~24090
    for _ in range(6):
        o, c = price, price - 25 * rng.uniform(0.9, 1.1)
        bars.append(_mk_bar(i, o, max(o, c) + 4, min(o, c) - 6, c))
        price = c
        i += 1
    trough1 = price
    # up leg 2: 8 bars +30/bar → ends ~24330
    for _ in range(8):
        o, c = price, price + 30 * rng.uniform(0.9, 1.1)
        bars.append(_mk_bar(i, o, max(o, c) + 6, min(o, c) - 4, c))
        price = c
        i += 1
    peak2 = price
    # pullback 2: engineered to land within 5 bps of trough1
    target = trough1 + rng.uniform(-8, 8)  # ~3 bps of 24000 = 7.2 pts
    steps = 6
    per_step = (target - price) / steps
    for _ in range(steps):
        o, c = price, price + per_step
        bars.append(_mk_bar(i, o, max(o, c) + 4, min(o, c) - 6, c))
        price = c
        i += 1
    # up leg 3 (small tail so trough2 is confirmed by find_swings)
    for _ in range(6):
        o, c = price, price + 25 * rng.uniform(0.9, 1.1)
        bars.append(_mk_bar(i, o, max(o, c) + 6, min(o, c) - 4, c))
        price = c
        i += 1
    return bars


def _gen_downtrend(rng: random.Random) -> list[Bar]:
    return _walk(
        [(-1, 8, 30), (+1, 6, 15), (-1, 8, 30), (+1, 6, 15), (-1, 8, 30)],
        rng,
    )


def _gen_downtrend_equal_highs(rng: random.Random) -> list[Bar]:
    """Mirror of uptrend_equal_lows — two rally-tops finish ≈ equal."""
    bars = []
    price = 24500.0
    i = 0
    for _ in range(8):
        o, c = price, price - 30 * rng.uniform(0.9, 1.1)
        bars.append(_mk_bar(i, o, max(o, c) + 6, min(o, c) - 4, c))
        price = c
        i += 1
    for _ in range(6):
        o, c = price, price + 25 * rng.uniform(0.9, 1.1)
        bars.append(_mk_bar(i, o, max(o, c) + 4, min(o, c) - 6, c))
        price = c
        i += 1
    top1 = price
    for _ in range(8):
        o, c = price, price - 30 * rng.uniform(0.9, 1.1)
        bars.append(_mk_bar(i, o, max(o, c) + 6, min(o, c) - 4, c))
        price = c
        i += 1
    target = top1 + rng.uniform(-8, 8)
    steps = 6
    per_step = (target - price) / steps
    for _ in range(steps):
        o, c = price, price + per_step
        bars.append(_mk_bar(i, o, max(o, c) + 6, min(o, c) - 4, c))
        price = c
        i += 1
    for _ in range(6):
        o, c = price, price - 25 * rng.uniform(0.9, 1.1)
        bars.append(_mk_bar(i, o, max(o, c) + 6, min(o, c) - 4, c))
        price = c
        i += 1
    return bars


def _gen_range(rng: random.Random) -> list[Bar]:
    """Sideways oscillation between ~24170 and ~24230. Both endpoints
    on each side should land in the equality band → NEUTRAL."""
    return _walk(
        [(+1, 5, 20), (-1, 5, 20), (+1, 5, 20), (-1, 5, 20), (+1, 5, 20), (-1, 5, 20)],
        rng,
    )


def _gen_choppy(rng: random.Random) -> list[Bar]:
    """Random walk with no directional bias — no confirmed swings likely,
    but where they occur, must not flip to CALL/PUT."""
    bars = []
    price = 24300.0
    for i in range(30):
        step = rng.uniform(-25, 25)
        o = price
        c = price + step
        h = max(o, c) + rng.uniform(3, 10)
        l = min(o, c) - rng.uniform(3, 10)
        bars.append(_mk_bar(i, o, h, l, c))
        price = c
    return bars


SCENARIOS = {
    "clean_uptrend":         (_gen_uptrend,                "CALL"),
    "uptrend_equal_lows":    (_gen_uptrend_equal_lows,     "CALL"),
    "clean_downtrend":       (_gen_downtrend,              "PUT"),
    "downtrend_equal_highs": (_gen_downtrend_equal_highs,  "PUT"),
    "sideways_range":        (_gen_range,                  "NEUTRAL"),
    "choppy_noise":          (_gen_choppy,                 "NEUTRAL"),
}


@dataclass
class Counter:
    call: int = 0
    put: int = 0
    neutral: int = 0

    def bump(self, verdict):
        if verdict == "CALL":
            self.call += 1
        elif verdict == "PUT":
            self.put += 1
        else:
            self.neutral += 1


def _pct(n, tot):
    return f"{(n / max(1, tot)) * 100:5.1f}%"


def run(seed: int = 7, n_per_scenario: int = 200) -> None:
    rng_master = random.Random(seed)
    print(f"\nR1 benchmark  seed={seed}  n_per_scenario={n_per_scenario}\n")
    header = f"{'scenario':<26} {'expected':<9}  {'OLD C/P/N':<20}  {'NEW C/P/N':<20}"
    print(header)
    print("-" * len(header))

    grand_old_neutral_trend = 0
    grand_new_neutral_trend = 0
    grand_trend = 0
    grand_old_flip_side = 0
    grand_new_flip_side = 0
    grand_side = 0
    grand_old_correct_trend = 0
    grand_new_correct_trend = 0

    for name, (gen, expected) in SCENARIOS.items():
        old_c = Counter()
        new_c = Counter()
        for _ in range(n_per_scenario):
            rng = random.Random(rng_master.random())
            bars = gen(rng)
            swings = find_swings(bars, lookback=SWING_WINDOW)
            old_v = OLD_detect_structure(swings)
            new_v = NEW_detect_structure(swings)
            old_c.bump(old_v)
            new_c.bump(new_v)

        tot = n_per_scenario
        if expected in ("CALL", "PUT"):
            grand_old_neutral_trend += old_c.neutral
            grand_new_neutral_trend += new_c.neutral
            grand_trend += tot
            old_correct = old_c.call if expected == "CALL" else old_c.put
            new_correct = new_c.call if expected == "CALL" else new_c.put
            grand_old_correct_trend += old_correct
            grand_new_correct_trend += new_correct
        else:
            old_flip = old_c.call + old_c.put
            new_flip = new_c.call + new_c.put
            grand_old_flip_side += old_flip
            grand_new_flip_side += new_flip
            grand_side += tot

        print(f"{name:<26} {expected:<9}  "
              f"{f'{old_c.call}/{old_c.put}/{old_c.neutral}':<20}  "
              f"{f'{new_c.call}/{new_c.put}/{new_c.neutral}':<20}")

    print("-" * len(header))
    print(f"\nTRENDING SCENARIOS (n={grand_trend}):")
    print(f"  OLD false-NEUTRAL:     {grand_old_neutral_trend}/{grand_trend}  ({_pct(grand_old_neutral_trend, grand_trend)})")
    print(f"  NEW false-NEUTRAL:     {grand_new_neutral_trend}/{grand_trend}  ({_pct(grand_new_neutral_trend, grand_trend)})")
    print(f"  OLD correct direction: {grand_old_correct_trend}/{grand_trend}  ({_pct(grand_old_correct_trend, grand_trend)})")
    print(f"  NEW correct direction: {grand_new_correct_trend}/{grand_trend}  ({_pct(grand_new_correct_trend, grand_trend)})")
    absolute = grand_old_neutral_trend - grand_new_neutral_trend
    reduction = (absolute / max(1, grand_old_neutral_trend)) * 100 if grand_old_neutral_trend else 0.0
    print(f"  False-NEUTRAL absolute reduction: {absolute}  ({reduction:.1f}%)")

    print(f"\nSIDEWAYS/CHOPPY (n={grand_side}):")
    print(f"  OLD directional flips (false positives): {grand_old_flip_side}  ({_pct(grand_old_flip_side, grand_side)})")
    print(f"  NEW directional flips (false positives): {grand_new_flip_side}  ({_pct(grand_new_flip_side, grand_side)})")

    ok = (
        grand_new_neutral_trend < grand_old_neutral_trend
        and grand_new_correct_trend >= grand_old_correct_trend
        and grand_new_flip_side <= grand_old_flip_side + max(2, int(0.01 * grand_side))
    )
    print("\nRESULT:", "PASS ✅  R1 reduces false-NEUTRAL without hurting sideways" if ok else "FAIL ❌")


if __name__ == "__main__":
    run()
