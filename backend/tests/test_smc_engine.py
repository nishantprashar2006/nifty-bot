"""tests/test_smc_engine.py
Pure-function tests for the v1.5 Smart Money Concepts engine (PART 2 spec).
Contract: given the same bars, always emit the same SMCResult — no
randomness, no hidden state, no AI."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.candle_manager import Bar
from data.swing_finder import find_swings
from strategy.smc_engine import (
    SWING_WINDOW,
    _wilder_atr,
    classify_strength,
    detect_displacement,
    detect_fvgs,
    detect_htf_trend,
    detect_order_blocks,
    detect_regime,
    detect_structure,
    evaluate,
)


def _bar(ts_min: int, o: float, h: float, l: float, c: float, v: int = 1000) -> Bar:
    ts = datetime(2026, 2, 17, 9, 20, tzinfo=timezone.utc) + timedelta(minutes=ts_min)
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def _trend(direction: str, n: int = 40, start: float = 22_000.0, step: float = 25.0) -> list[Bar]:
    """Mostly-monotonic zig-zag with confirmable HH/HL or LH/LL structure."""
    bars: list[Bar] = []
    px = start
    for i in range(n):
        if direction == "up":
            px += step
            o, h, l, c = px - step / 2, px + 5, px - 5, px
        else:
            px -= step
            o, h, l, c = px + step / 2, px + 5, px - 5, px
        bars.append(_bar(i, o, h, l, c))
    return bars


def _zigzag_up(n: int = 50) -> list[Bar]:
    """Synthetic 5m bars with clearly confirmable HH+HL fractal swings
    (peaks at bar 5 & 25, troughs at bar 15 & 35) for lookback=5.
    Default 50 bars so both swings have 5 bars confirming on the right."""
    # Custom-built closes so peaks/troughs are strict fractal extremes
    closes = [
        100, 101, 102, 103, 104,    # 0-4 rising
        108,                         # 5 PEAK (high=110)
        103, 102, 101, 100, 99,     # 6-10 falling
        98, 97, 96, 95,             # 11-14
        91,                          # 15 TROUGH (low=85)
        95, 97, 99, 100, 101,        # 16-20 rising
        103, 104, 105, 106,          # 21-24
        113,                         # 25 HIGHER PEAK (high=115)
        106, 105, 104, 103, 102,     # 26-30
        100, 99, 98, 97,             # 31-34
        93,                          # 35 HIGHER TROUGH (low=88)
        96, 98, 100, 102, 104,       # 36-40
        105, 106, 107, 108, 109,     # 41-45
        110, 111, 112, 113,          # 46-49
    ]
    closes = (closes + [110] * n)[:n]
    bars: list[Bar] = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        if i == 5:
            h, l = 110, min(o, c) - 1
        elif i == 25:
            h, l = 115, min(o, c) - 1
        elif i == 15:
            h, l = max(o, c) + 1, 85       # deep trough
        elif i == 35:
            h, l = max(o, c) + 1, 88       # higher trough but still strictly below neighbours
        else:
            h = max(o, c) + 1
            l = min(o, c) - 1
        bars.append(_bar(i, o, h, l, c))
    return bars


# ─────────────────────────────────────────── unit cases
def test_warming_up_when_insufficient_bars():
    res = evaluate([], [])
    assert res.direction == "NEUTRAL"
    assert res.confidence == 0
    assert res.reasons == ["warming_up"]


def test_swing_window_default_is_five():
    assert SWING_WINDOW == 5


def test_htf_trend_call_on_uptrend_structure():
    # PART 2: HTF must be structure-based, not EMA-based.
    bars = _zigzag_up()
    assert detect_htf_trend(bars) == "CALL"


def test_htf_trend_neutral_on_flat():
    bars = [_bar(i, 100, 101, 99, 100) for i in range(40)]
    assert detect_htf_trend(bars) == "NEUTRAL"


def test_structure_detects_hh_hl():
    bars = _zigzag_up()
    swings = find_swings(bars, lookback=SWING_WINDOW)
    assert any(s.side == "HIGH" for s in swings)
    assert any(s.side == "LOW" for s in swings)
    assert detect_structure(swings) == "CALL"


def test_fvg_bullish_detected():
    bars = [
        _bar(0, 100, 102, 99, 100),
        _bar(1, 102, 108, 102, 107),
        _bar(2, 108, 110, 105, 109),   # low (105) > prev high (102) → bull FVG
    ]
    gaps = detect_fvgs(bars)
    assert any(g.side == "BULL" for g in gaps)


def test_displacement_bull_when_body_exceeds_atr_mult():
    # ATR ≈ 1, body must exceed 1.5
    atr = 1.0
    bull = _bar(0, 100.0, 105.0, 99.5, 104.8)   # body 4.8 > 1.5, close near high
    bear = _bar(1, 105.0, 105.2, 99.0, 99.2)    # body 5.8 > 1.5, close near low
    small = _bar(2, 100.0, 101.0, 99.0, 100.5)  # body 0.5
    assert detect_displacement(bull, atr) == "BULL"
    assert detect_displacement(bear, atr) == "BEAR"
    assert detect_displacement(small, atr) is None


def test_displacement_requires_close_near_extreme():
    """Body big but close in middle of range → not a displacement."""
    atr = 1.0
    # body 4, range 10, close in middle → rejected
    mid = _bar(0, 100.0, 110.0, 99.0, 104.0)
    assert detect_displacement(mid, atr) is None


def test_order_block_bullish_via_displacement():
    """OB = last opposite-color candle before a confirmed displacement."""
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 101, 99, 100),
        _bar(2, 100, 101, 99, 100),
        _bar(3, 100, 101, 99, 100),
        _bar(4, 100, 101, 99, 100),
        _bar(5, 100, 101, 99, 100),
        _bar(6, 100, 101, 99, 100),
        _bar(7, 100, 101, 99, 100),
        _bar(8, 100, 101, 99, 100),
        _bar(9, 100, 101, 99, 100),
        _bar(10, 100, 101, 99, 100),
        _bar(11, 100, 101, 99, 100),
        _bar(12, 100, 101, 99, 100),
        _bar(13, 100, 101, 99, 100),
        _bar(14, 100, 101, 99, 100),     # ATR ≈ 2
        _bar(15, 100, 100.5, 98, 98.5),  # bearish candle — OB candidate
        _bar(16, 98.5, 108, 98, 107.8),  # bull displacement: body 9.3 > 2×1.5=3
        _bar(17, 107, 110, 106, 109),
    ]
    atr = _wilder_atr(bars)
    obs = detect_order_blocks(bars, atr)
    assert any(o.side == "BULL" for o in obs)


def test_classify_strength_buckets():
    assert classify_strength(90) == "STRONG"
    assert classify_strength(70) == "GOOD"
    assert classify_strength(50) == "NEUTRAL"
    assert classify_strength(25) == "WEAK"
    assert classify_strength(0) == "AVOID"


def test_evaluate_is_deterministic():
    bars_5m = _trend("up", 40)
    bars_15m = _trend("up", 25)
    a = evaluate(bars_5m, bars_15m)
    b = evaluate(bars_5m, bars_15m)
    assert a.direction == b.direction
    assert a.confidence == b.confidence
    assert a.reasons == b.reasons
    assert a.entry == b.entry
    assert a.stop_loss == b.stop_loss
    assert a.target == b.target


def test_evaluate_confidence_bounded():
    bars_5m = _trend("up", 80)
    bars_15m = _trend("up", 40)
    res = evaluate(bars_5m, bars_15m)
    assert 0 <= res.confidence <= 100


def test_evaluate_ctx_exposes_structure_and_htf():
    bars_5m = _zigzag_up()
    bars_15m = _zigzag_up()
    res = evaluate(bars_5m, bars_15m)
    assert res.ctx is not None
    # required dashboard fields exist
    assert res.ctx.htf_trend in {"CALL", "PUT", "NEUTRAL"}
    assert res.ctx.structure in {"CALL", "PUT", "NEUTRAL"}
    assert res.ctx.regime in {"TRENDING", "SIDEWAYS", "HIGH_VOL", "LOW_VOL", "UNCLEAR"}


def test_regime_classifier_returns_valid_label():
    bars = _trend("up", 40)
    swings = find_swings(bars, lookback=SWING_WINDOW)
    atr = _wilder_atr(bars)
    reg = detect_regime(bars, swings, atr)
    assert reg in {"TRENDING", "SIDEWAYS", "HIGH_VOL", "LOW_VOL", "UNCLEAR"}


def test_regime_attenuates_confidence_in_sideways():
    """Sideways/HighVol/LowVol should never increase score above 100,
    and a flat market should not produce STRONG signals."""
    flat = [_bar(i, 100, 100.5, 99.5, 100) for i in range(40)]
    res = evaluate(flat, flat[:20])
    assert res.confidence <= 30


# ─────────────────────────── Widened recent-event windows (PART 3 tweak)
def test_liquidity_sweep_detects_within_recent_window():
    """A sweep that fired 2 bars ago should still be surfaced within the
    RECENT_EVENT_BARS window (default 3 bars ≈ 15 min on 5m). Verifies we
    don't lose valid sweep credit on the very next candle."""
    from strategy.smc_engine import RECENT_EVENT_BARS, detect_liquidity_sweep
    from data.swing_finder import Swing
    assert RECENT_EVENT_BARS >= 2   # widened from 1
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 101, 99, 100),
        _bar(2, 101, 108, 100, 107),
        _bar(3, 107, 108, 106, 107),
        _bar(4, 107, 109, 93, 108),    # ← sweep of the low at 95 (wick to 93, close back)
        _bar(5, 108, 109, 107, 108),   # 1 bar after sweep — still fresh
        _bar(6, 108, 109, 107, 108),   # 2 bars after — still within window
    ]
    swings = [
        Swing(idx=3, price=95.0, side="LOW", ts=bars[3].ts),
        Swing(idx=1, price=110.0, side="HIGH", ts=bars[1].ts),
    ]
    assert detect_liquidity_sweep(bars, swings) == "CALL"


def test_liquidity_sweep_expires_after_window():
    """Sweeps older than RECENT_EVENT_BARS should stop being credited —
    we're not letting stale events pollute current confidence."""
    from strategy.smc_engine import RECENT_EVENT_BARS, detect_liquidity_sweep
    from data.swing_finder import Swing
    bars = [_bar(0, 100, 108, 92, 108)]   # sweep bar
    # Pad with N+2 flat bars to push the sweep out of the recent window
    for i in range(1, RECENT_EVENT_BARS + 3):
        bars.append(_bar(i, 108, 109, 107, 108))
    swings = [Swing(idx=0, price=95.0, side="LOW", ts=bars[0].ts)]
    # The sweep candle at idx 0 is way outside the last RECENT_EVENT_BARS window
    assert detect_liquidity_sweep(bars, swings) is None


# ─────────────────────────── Near-OTM strike selection
def test_option_selector_near_otm_strike_math():
    """Near-OTM selection contract (independent of scrip-master noise):
      • CE strike = smallest 50-multiple STRICTLY greater than spot
      • PE strike = largest  50-multiple STRICTLY less    than spot
    Boundary case: when spot IS an exact 50-multiple, both legs push
    outward by an additional 50 (no ATM overlap)."""
    import math
    step = 50

    def near_otm(spot: float) -> tuple[int, int]:
        ce = math.floor(spot / step) * step + step
        pe = math.ceil(spot / step) * step - step
        if ce - spot <= 0: ce += step
        if spot - pe <= 0: pe -= step
        return ce, pe

    assert near_otm(24555) == (24600, 24550)   # spot 24555 → CE 24600, PE 24550
    assert near_otm(24500) == (24550, 24450)   # exact strike → push both out
    assert near_otm(24501) == (24550, 24500)
    assert near_otm(24549) == (24550, 24500)
    assert near_otm(24550) == (24600, 24500)   # exact strike again


# ─────────────────────────── HTF: last-3 endpoint majority
def test_structure_last3_ignores_middle_anomaly():
    """One noisy middle swing should no longer force NEUTRAL.
    Endpoint check `highs[-1] > highs[-3]` and `lows[-1] > lows[-3]`."""
    from data.swing_finder import Swing
    from strategy.smc_engine import detect_structure
    from datetime import datetime, timezone
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Highs march up cleanly: 100 → 110 → 120
    # Lows: 90 → 85 (dip) → 95   ← middle low broke pattern under old rule
    swings = [
        Swing(idx=0,  price=100, side="HIGH", ts=ts),
        Swing(idx=1,  price=90,  side="LOW",  ts=ts),
        Swing(idx=2,  price=110, side="HIGH", ts=ts),
        Swing(idx=3,  price=85,  side="LOW",  ts=ts),   # anomalous dip
        Swing(idx=4,  price=120, side="HIGH", ts=ts),
        Swing(idx=5,  price=95,  side="LOW",  ts=ts),
    ]
    assert detect_structure(swings) == "CALL"


def test_structure_last3_falls_back_to_last2_when_scarce():
    """When only 2 confirmed swings per side exist, use the strict last-2
    rule (preserves early-session warm-up behaviour)."""
    from data.swing_finder import Swing
    from strategy.smc_engine import detect_structure
    from datetime import datetime, timezone
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    swings = [
        Swing(idx=0, price=100, side="HIGH", ts=ts),
        Swing(idx=1, price=90,  side="LOW",  ts=ts),
        Swing(idx=2, price=110, side="HIGH", ts=ts),
        Swing(idx=3, price=95,  side="LOW",  ts=ts),
    ]
    assert detect_structure(swings) == "CALL"


def test_structure_last3_stays_neutral_on_flat_endpoints():
    """If highs endpoints don't advance, the answer must remain NEUTRAL —
    the last-3 rule must not fabricate a trend."""
    from data.swing_finder import Swing
    from strategy.smc_engine import detect_structure
    from datetime import datetime, timezone
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # highs: 100 → 115 (up middle) → 100 (back to same endpoint)
    # lows:  90 → 85 (dip) → 90
    swings = [
        Swing(idx=0, price=100, side="HIGH", ts=ts),
        Swing(idx=1, price=90,  side="LOW",  ts=ts),
        Swing(idx=2, price=115, side="HIGH", ts=ts),
        Swing(idx=3, price=85,  side="LOW",  ts=ts),
        Swing(idx=4, price=100, side="HIGH", ts=ts),
        Swing(idx=5, price=90,  side="LOW",  ts=ts),
    ]
    assert detect_structure(swings) == "NEUTRAL"
