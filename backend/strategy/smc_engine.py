"""
strategy/smc_engine.py
======================
Smart Money Concepts engine — version 1.

Strictly deterministic. No AI, no probability models, no discretionary logic.
Given identical input bars, produces an identical signal & confidence.

Concepts implemented (and ONLY these):
    • Order Blocks (OB)
    • Fair Value Gaps (FVG)
    • Break of Structure (BOS)
    • Change of Character (CHoCH)
    • Liquidity Sweeps
    • Premium / Discount Zones
    • Equal Highs / Equal Lows (EQH/EQL) — used as liquidity references

Confidence scoring (max 100):
    HTF Trend Alignment ............... 20
    Market Structure (HH/HL | LH/LL) .. 15
    BOS / CHoCH ....................... 20
    Liquidity Sweep ................... 15
    Order Block Retest ................ 15
    Fair Value Gap Mitigation ......... 10
    Premium / Discount Alignment ......  5
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from data.candle_manager import Bar
from data.swing_finder import Swing, find_swings


Direction = Literal["CALL", "PUT", "NEUTRAL"]


# ───────────────────────────── primitives ──────────────────────────────
@dataclass(frozen=True)
class OrderBlock:
    side: Literal["BULL", "BEAR"]
    high: float
    low: float
    idx: int


@dataclass(frozen=True)
class FVG:
    side: Literal["BULL", "BEAR"]
    top: float
    bottom: float
    idx: int          # index of the middle (impulse) bar
    mitigated: bool   # has price re-entered the gap since?


@dataclass
class SMCContext:
    htf_trend: Direction = "NEUTRAL"
    structure: Direction = "NEUTRAL"        # HH/HL = CALL, LH/LL = PUT
    bos: Optional[Direction] = None
    choch: Optional[Direction] = None
    sweep: Optional[Direction] = None        # direction the sweep favours
    ob_retest: Optional[OrderBlock] = None
    fvg_mitigation: Optional[FVG] = None
    premium_discount: Literal["PREMIUM", "DISCOUNT", "MID"] = "MID"
    eqh: bool = False
    eql: bool = False
    last_close: Optional[float] = None
    range_high: Optional[float] = None
    range_low: Optional[float] = None


@dataclass
class SMCResult:
    direction: Direction
    confidence: int
    reasons: list[str] = field(default_factory=list)
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    ctx: Optional[SMCContext] = None


# ──────────────────────────── detectors ────────────────────────────────
def detect_htf_trend(bars_15m: list[Bar]) -> Direction:
    """Higher-timeframe trend = last close vs simple 20-bar EMA on 15m."""
    if len(bars_15m) < 20:
        return "NEUTRAL"
    closes = [b.close for b in bars_15m]
    alpha = 2 / (20 + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = alpha * c + (1 - alpha) * ema
    last = closes[-1]
    if last > ema * 1.0005:
        return "CALL"
    if last < ema * 0.9995:
        return "PUT"
    return "NEUTRAL"


def detect_structure(swings: list[Swing]) -> Direction:
    """HH+HL → CALL · LH+LL → PUT · else NEUTRAL. Uses last 2 highs + 2 lows."""
    highs = [s for s in swings if s.side == "HIGH"][-2:]
    lows = [s for s in swings if s.side == "LOW"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"
    hh = highs[1].price > highs[0].price
    hl = lows[1].price > lows[0].price
    lh = highs[1].price < highs[0].price
    ll = lows[1].price < lows[0].price
    if hh and hl:
        return "CALL"
    if lh and ll:
        return "PUT"
    return "NEUTRAL"


def detect_bos_choch(
    swings: list[Swing], bars: list[Bar], structure: Direction
) -> tuple[Optional[Direction], Optional[Direction]]:
    """BOS = close beyond prior same-direction swing. CHoCH = first BOS in
    opposite direction relative to the current structure."""
    if not bars or len(swings) < 2:
        return None, None
    last_close = bars[-1].close
    last_high = max((s for s in swings if s.side == "HIGH"), key=lambda s: s.idx, default=None)
    last_low = max((s for s in swings if s.side == "LOW"), key=lambda s: s.idx, default=None)
    bos: Optional[Direction] = None
    if last_high and last_close > last_high.price:
        bos = "CALL"
    elif last_low and last_close < last_low.price:
        bos = "PUT"
    choch: Optional[Direction] = None
    if bos and structure != "NEUTRAL" and bos != structure:
        choch = bos
    return bos, choch


def detect_liquidity_sweep(bars: list[Bar], swings: list[Swing]) -> Optional[Direction]:
    """Wick beyond a recent swing then close back inside it (bull or bear)."""
    if not bars or len(swings) < 1:
        return None
    last = bars[-1]
    recent_highs = [s for s in swings[-6:] if s.side == "HIGH"]
    recent_lows = [s for s in swings[-6:] if s.side == "LOW"]
    for s in recent_highs:
        if last.high > s.price and last.close < s.price:
            return "PUT"   # swept buy-side liquidity → bearish
    for s in recent_lows:
        if last.low < s.price and last.close > s.price:
            return "CALL"  # swept sell-side liquidity → bullish
    return None


def detect_order_blocks(bars: list[Bar], lookback: int = 30) -> list[OrderBlock]:
    """Order block = last opposite-color candle before a 3-bar strong impulse."""
    out: list[OrderBlock] = []
    start = max(2, len(bars) - lookback)
    for i in range(start, len(bars) - 3):
        a, b, c = bars[i], bars[i + 1], bars[i + 2]
        impulse_up = b.close > b.open and c.close > c.open and c.close > a.high
        impulse_dn = b.close < b.open and c.close < c.open and c.close < a.low
        if impulse_up and a.close < a.open:
            out.append(OrderBlock("BULL", a.high, a.low, i))
        elif impulse_dn and a.close > a.open:
            out.append(OrderBlock("BEAR", a.high, a.low, i))
    return out


def detect_fvgs(bars: list[Bar], lookback: int = 30) -> list[FVG]:
    """3-candle FVG: bullish if bar[i+2].low > bar[i].high; bearish mirror."""
    out: list[FVG] = []
    start = max(0, len(bars) - lookback)
    for i in range(start, len(bars) - 2):
        a, _, c = bars[i], bars[i + 1], bars[i + 2]
        if c.low > a.high:
            top, bottom = c.low, a.high
            mitigated = any(bars[k].low <= top and bars[k].high >= bottom
                            for k in range(i + 3, len(bars)))
            out.append(FVG("BULL", top, bottom, i + 1, mitigated))
        elif c.high < a.low:
            top, bottom = a.low, c.high
            mitigated = any(bars[k].low <= top and bars[k].high >= bottom
                            for k in range(i + 3, len(bars)))
            out.append(FVG("BEAR", top, bottom, i + 1, mitigated))
    return out


def detect_ob_retest(bars: list[Bar], obs: list[OrderBlock]) -> Optional[OrderBlock]:
    """Latest unmitigated OB whose zone has been touched by the current bar."""
    if not bars or not obs:
        return None
    last = bars[-1]
    for ob in reversed(obs):
        if ob.low <= last.low <= ob.high or ob.low <= last.high <= ob.high:
            return ob
    return None


def detect_eq_levels(swings: list[Swing], tol_bps: float = 5.0) -> tuple[bool, bool]:
    """EQH/EQL — two same-side swings within `tol_bps` of each other."""
    highs = [s for s in swings if s.side == "HIGH"][-3:]
    lows = [s for s in swings if s.side == "LOW"][-3:]

    def near(a: float, b: float) -> bool:
        return abs(a - b) / max(a, 1) * 10_000 <= tol_bps

    eqh = len(highs) >= 2 and near(highs[-1].price, highs[-2].price)
    eql = len(lows) >= 2 and near(lows[-1].price, lows[-2].price)
    return eqh, eql


def detect_premium_discount(bars: list[Bar], swings: list[Swing]) -> tuple[str, float, float]:
    """Compute premium/discount within the most recent impulse leg
    (last swing low → last swing high or vice-versa). Returns
    (zone, range_high, range_low)."""
    if not bars or len(swings) < 2:
        return "MID", 0.0, 0.0
    last_high = max((s for s in swings if s.side == "HIGH"), key=lambda s: s.idx, default=None)
    last_low = max((s for s in swings if s.side == "LOW"), key=lambda s: s.idx, default=None)
    if not last_high or not last_low:
        return "MID", 0.0, 0.0
    rng_hi = last_high.price
    rng_lo = last_low.price
    if rng_hi <= rng_lo:
        return "MID", rng_hi, rng_lo
    mid = (rng_hi + rng_lo) / 2
    last_close = bars[-1].close
    if last_close > mid:
        return "PREMIUM", rng_hi, rng_lo
    if last_close < mid:
        return "DISCOUNT", rng_hi, rng_lo
    return "MID", rng_hi, rng_lo


# ────────────────────────── main scoring engine ─────────────────────────
def evaluate(bars_5m: list[Bar], bars_15m: list[Bar]) -> SMCResult:
    """Run all detectors, score, and emit a signal. Pure function — given the
    same inputs it always produces the same output."""
    if len(bars_5m) < 20 or len(bars_15m) < 10:
        return SMCResult("NEUTRAL", 0, ["warming_up"], None, None, None, SMCContext())

    swings = find_swings(bars_5m, lookback=2)
    ctx = SMCContext()
    ctx.last_close = bars_5m[-1].close
    ctx.htf_trend = detect_htf_trend(bars_15m)
    ctx.structure = detect_structure(swings)
    ctx.bos, ctx.choch = detect_bos_choch(swings, bars_5m, ctx.structure)
    ctx.sweep = detect_liquidity_sweep(bars_5m, swings)
    obs = detect_order_blocks(bars_5m)
    ctx.ob_retest = detect_ob_retest(bars_5m, obs)
    fvgs = detect_fvgs(bars_5m)
    unmitigated = [f for f in fvgs if not f.mitigated]
    ctx.fvg_mitigation = unmitigated[-1] if unmitigated else None
    ctx.eqh, ctx.eql = detect_eq_levels(swings)
    zone, rng_hi, rng_lo = detect_premium_discount(bars_5m, swings)
    ctx.premium_discount = zone  # type: ignore[assignment]
    ctx.range_high, ctx.range_low = rng_hi, rng_lo

    # Score each side independently using the user's weights
    call_score = 0
    put_score = 0
    reasons_call: list[str] = []
    reasons_put: list[str] = []

    if ctx.htf_trend == "CALL":
        call_score += 20; reasons_call.append("HTF trend CALL (+20)")
    elif ctx.htf_trend == "PUT":
        put_score += 20; reasons_put.append("HTF trend PUT (+20)")

    if ctx.structure == "CALL":
        call_score += 15; reasons_call.append("Structure HH+HL (+15)")
    elif ctx.structure == "PUT":
        put_score += 15; reasons_put.append("Structure LH+LL (+15)")

    if ctx.bos == "CALL" or ctx.choch == "CALL":
        call_score += 20
        reasons_call.append(f"{'CHoCH' if ctx.choch == 'CALL' else 'BOS'} bullish (+20)")
    elif ctx.bos == "PUT" or ctx.choch == "PUT":
        put_score += 20
        reasons_put.append(f"{'CHoCH' if ctx.choch == 'PUT' else 'BOS'} bearish (+20)")

    if ctx.sweep == "CALL":
        call_score += 15; reasons_call.append("Sell-side sweep (+15)")
    elif ctx.sweep == "PUT":
        put_score += 15; reasons_put.append("Buy-side sweep (+15)")

    if ctx.ob_retest:
        if ctx.ob_retest.side == "BULL":
            call_score += 15; reasons_call.append("Bull OB retest (+15)")
        else:
            put_score += 15; reasons_put.append("Bear OB retest (+15)")

    if ctx.fvg_mitigation:
        if ctx.fvg_mitigation.side == "BULL":
            call_score += 10; reasons_call.append("Bull FVG mitigation (+10)")
        else:
            put_score += 10; reasons_put.append("Bear FVG mitigation (+10)")

    # Premium/discount alignment — buy in discount, sell in premium
    if ctx.premium_discount == "DISCOUNT":
        call_score += 5; reasons_call.append("In discount zone (+5)")
    elif ctx.premium_discount == "PREMIUM":
        put_score += 5; reasons_put.append("In premium zone (+5)")

    # Pick the dominant direction; ties → NEUTRAL
    if call_score == put_score:
        return SMCResult("NEUTRAL", max(call_score, put_score),
                         reasons_call + reasons_put, None, None, None, ctx)

    direction: Direction = "CALL" if call_score > put_score else "PUT"
    confidence = max(call_score, put_score)
    reasons = reasons_call if direction == "CALL" else reasons_put

    # Entry / SL / TP — anchored to the last close + range_high/low
    entry = ctx.last_close
    if direction == "CALL":
        stop_loss = ctx.range_low or (entry * 0.995)
        risk = entry - stop_loss
        target = entry + 2 * risk if risk > 0 else entry * 1.01
    else:
        stop_loss = ctx.range_high or (entry * 1.005)
        risk = stop_loss - entry
        target = entry - 2 * risk if risk > 0 else entry * 0.99

    return SMCResult(direction, confidence, reasons,
                     round(entry, 2), round(stop_loss, 2), round(target, 2), ctx)


def classify_strength(confidence: int) -> str:
    if confidence >= 80: return "STRONG"
    if confidence >= 60: return "GOOD"
    if confidence >= 40: return "NEUTRAL"
    if confidence >= 20: return "WEAK"
    return "AVOID"
