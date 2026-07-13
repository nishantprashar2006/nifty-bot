"""
Regression tests for the R1 refinement to `detect_structure`:
    EQ_TOLERANCE_BPS-aware endpoint comparison.

R1 rule: equal endpoints on one side (within EQ_TOLERANCE_BPS = 5 bps)
are treated as "flat on that side", not as a directional break.
The OTHER side must still be strictly directional for a CALL/PUT verdict.

Tests use NIFTY-scale prices (~24000) so the 5-bps tolerance (0.05% ≈ 12 pts)
matches real intraday behaviour.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.swing_finder import Swing  # noqa: E402
from strategy.smc_engine import (  # noqa: E402
    EQ_TOLERANCE_BPS,
    _bps_diff,
    detect_structure,
)

_TS = datetime(2026, 2, 17, tzinfo=timezone.utc)


def _hs(prices):
    """Build alternating HIGH/LOW swings from a flat list ordered
    HIGH, LOW, HIGH, LOW, ..."""
    out = []
    for i, p in enumerate(prices):
        side = "HIGH" if i % 2 == 0 else "LOW"
        out.append(Swing(idx=i, price=p, side=side, ts=_TS))
    return out


# ────────────────────────────────────── _bps_diff correctness
def test_bps_diff_zero_for_identical_prices():
    assert _bps_diff(24000, 24000) == 0.0


def test_bps_diff_within_tolerance_for_5_bps_gap():
    # 5 bps of 24000 midpoint = 12 pts. 24000 vs 24012 → exactly at tolerance.
    assert _bps_diff(24000, 24012) <= EQ_TOLERANCE_BPS + 1e-6


def test_bps_diff_exceeds_tolerance_for_10_bps_gap():
    assert _bps_diff(24000, 24024) > EQ_TOLERANCE_BPS


# ────────────────────────────────────── R1 core scenarios (NIFTY-scale)
def test_scenario_A_equal_low_pullback_in_uptrend_now_call():
    """R1 FIX — highs strictly up, most-recent pullback low equals prior low.
    Old detector: NEUTRAL. New detector: CALL."""
    swings = _hs([24000, 23900, 24100, 23850, 24200, 23900])
    # highs endpoints: 24000 → 24200 (>5 bps → strict up)
    # lows endpoints:  23900 → 23900 (exactly equal → in tolerance)
    assert detect_structure(swings) == "CALL"


def test_scenario_A_within_tolerance_low_also_call():
    """Lows within tolerance (not exactly equal) still counted as ‘flat’."""
    swings = _hs([24000, 23900, 24100, 23850, 24200, 23905])
    # lows: 23900 → 23905 → |Δ|=5 pts on 23902 midpoint ≈ 2 bps < 5 bps tol.
    assert _bps_diff(23905, 23900) <= EQ_TOLERANCE_BPS
    assert detect_structure(swings) == "CALL"


def test_scenario_A_mirror_equal_high_in_downtrend_now_put():
    """Mirror of Scenario A — lows strictly down, highs equal at endpoints."""
    swings = _hs([24200, 23900, 24200, 23800, 24200, 23700])
    # highs endpoints: 24200 → 24200 (equal); lows: 23900 → 23700 (strict down)
    assert detect_structure(swings) == "PUT"


def test_scenario_D_genuine_range_still_neutral():
    """Both endpoint pairs land in the equality band → NEUTRAL. Anti-whipsaw preserved."""
    swings = _hs([24100, 23900, 24150, 23950, 24100, 23900])
    # highs: 24100 → 24100 (equal); lows: 23900 → 23900 (equal)
    assert detect_structure(swings) == "NEUTRAL"


def test_broadening_pattern_still_neutral():
    """Highs strictly up + lows strictly down (broadening) → NEUTRAL."""
    swings = _hs([24000, 23900, 24100, 23800, 24200, 23700])
    assert detect_structure(swings) == "NEUTRAL"


def test_classic_uptrend_still_call():
    """Both sides strictly bullish — must still be CALL (no regression)."""
    swings = _hs([24000, 23900, 24100, 23950, 24200, 24000])
    assert detect_structure(swings) == "CALL"


def test_classic_downtrend_still_put():
    swings = _hs([24200, 24000, 24100, 23950, 24000, 23850])
    assert detect_structure(swings) == "PUT"


def test_warmup_fallback_last2_still_works():
    """Only 2 swings per side — last-2 rule with tolerance."""
    swings = _hs([24000, 23900, 24100, 23950])
    assert detect_structure(swings) == "CALL"


def test_warmup_fallback_equal_low_now_call():
    """R1 also applies to the warm-up last-2 fallback — equal low + rising
    highs → CALL (used to be NEUTRAL)."""
    swings = _hs([24000, 23900, 24100, 23900])
    # highs: 24000 → 24100 (>5 bps up); lows: 23900 → 23900 (equal)
    assert detect_structure(swings) == "CALL"


def test_insufficient_swings_returns_neutral():
    swings = _hs([24000, 23900])
    assert detect_structure(swings) == "NEUTRAL"


def test_one_bearish_side_kills_call():
    """Highs equal, lows strictly down — must be PUT (l_dn wins, no h_up).
    But we assert the anti-CALL side of it: NOT CALL."""
    swings = _hs([24000, 23900, 24000, 23800, 24000, 23700])
    result = detect_structure(swings)
    assert result != "CALL"
    assert result == "PUT"


def test_barely_above_tolerance_registers_as_direction():
    """Endpoint gap just past 5 bps → treated as strict direction, not equal."""
    # 24000 vs 24025 → |Δ| = 25 pts on ~24012.5 midpoint = 10.4 bps > 5 bps
    swings = _hs([24000, 23900, 24010, 23950, 24025, 24000])
    assert _bps_diff(24025, 24000) > EQ_TOLERANCE_BPS
    assert detect_structure(swings) == "CALL"


def test_htf_pending_when_no_swings():
    """Empty swings input must return NEUTRAL, not crash."""
    assert detect_structure([]) == "NEUTRAL"
