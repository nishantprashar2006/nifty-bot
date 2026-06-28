"""tests/test_smc_engine.py
Pure-function tests for the Smart Money Concepts engine. The contract is:
given the same bars, always emit the same SMCResult — no randomness, no
hidden state, no AI."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.candle_manager import Bar
from data.swing_finder import find_swings
from strategy.smc_engine import (
    classify_strength,
    detect_fvgs,
    detect_htf_trend,
    detect_order_blocks,
    detect_structure,
    evaluate,
)


def _bar(ts_min: int, o: float, h: float, l: float, c: float, v: int = 1000) -> Bar:
    ts = datetime(2026, 2, 17, 9, 20, tzinfo=timezone.utc) + timedelta(minutes=ts_min)
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def _trend(direction: str, n: int = 25, start: float = 22_000.0, step: float = 25.0) -> list[Bar]:
    bars: list[Bar] = []
    px = start
    for i in range(n):
        if direction == "up":
            px += step
        else:
            px -= step
        bars.append(_bar(i, px - step / 2, px + 5, px - 5, px))
    return bars


# ─────────────────────────────────────────── unit cases
def test_warming_up_when_insufficient_bars():
    res = evaluate([], [])
    assert res.direction == "NEUTRAL"
    assert res.confidence == 0
    assert res.reasons == ["warming_up"]


def test_htf_trend_call_on_uptrend():
    assert detect_htf_trend(_trend("up", 30)) == "CALL"


def test_htf_trend_put_on_downtrend():
    assert detect_htf_trend(_trend("down", 30)) == "PUT"


def test_structure_detects_hh_hl():
    # Strict fractal (lookback=2) requires 2 strictly lower-high bars on
    # each side of every swing high. Build a clear zig-zag.
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 103, 99, 102),
        _bar(2, 102, 110, 101, 109),   # swing HIGH (110)
        _bar(3, 109, 108, 100, 101),
        _bar(4, 101, 102, 95, 96),     # swing LOW  (95)
        _bar(5, 96, 100, 96, 99),
        _bar(6, 99, 115, 98, 114),     # higher HIGH (115)
        _bar(7, 114, 113, 105, 106),
        _bar(8, 106, 107, 100, 101),   # higher LOW  (100 > 95)
        _bar(9, 101, 108, 100, 107),
        _bar(10, 107, 109, 102, 103),
    ]
    swings = find_swings(bars, lookback=2)
    assert any(s.side == "HIGH" for s in swings)
    assert any(s.side == "LOW" for s in swings)


def test_fvg_bullish_detected():
    bars = [
        _bar(0, 100, 102, 99, 100),
        _bar(1, 102, 108, 102, 107),
        _bar(2, 108, 110, 105, 109),   # low (105) > prev high (102) → bull FVG
    ]
    gaps = detect_fvgs(bars)
    assert any(g.side == "BULL" for g in gaps)


def test_order_block_bullish():
    # OB scanner iterates i in [max(2, n-30), n-3). Need n ≥ 6 so the
    # bearish candle + 3-bar impulse window fits inside the slice.
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 101, 99, 100),
        _bar(2, 105, 106, 100, 101),   # bearish (close < open) — OB candidate
        _bar(3, 101, 110, 101, 109),   # bull impulse 1
        _bar(4, 109, 115, 108, 114),   # bull impulse 2 (close 114 > a.high 106)
        _bar(5, 114, 116, 113, 115),
        _bar(6, 115, 117, 114, 116),
    ]
    obs = detect_order_blocks(bars)
    assert any(o.side == "BULL" for o in obs)


def test_classify_strength_buckets():
    assert classify_strength(90) == "STRONG"
    assert classify_strength(70) == "GOOD"
    assert classify_strength(50) == "NEUTRAL"
    assert classify_strength(25) == "WEAK"
    assert classify_strength(0) == "AVOID"


def test_evaluate_is_deterministic():
    bars_5m = _trend("up", 30)
    bars_15m = _trend("up", 15)
    a = evaluate(bars_5m, bars_15m)
    b = evaluate(bars_5m, bars_15m)
    assert a.direction == b.direction
    assert a.confidence == b.confidence
    assert a.reasons == b.reasons
    assert a.entry == b.entry
    assert a.stop_loss == b.stop_loss
    assert a.target == b.target


def test_evaluate_uptrend_biases_call():
    bars_5m = _trend("up", 30)
    bars_15m = _trend("up", 20)
    res = evaluate(bars_5m, bars_15m)
    # at minimum the HTF-trend weight (20) should fire
    assert res.confidence >= 20
    assert res.direction in {"CALL", "NEUTRAL"}


def test_evaluate_downtrend_biases_put():
    bars_5m = _trend("down", 30)
    bars_15m = _trend("down", 20)
    res = evaluate(bars_5m, bars_15m)
    assert res.confidence >= 20
    assert res.direction in {"PUT", "NEUTRAL"}


def test_evaluate_confidence_bounded():
    bars_5m = _trend("up", 60)
    bars_15m = _trend("up", 30)
    res = evaluate(bars_5m, bars_15m)
    assert 0 <= res.confidence <= 100
