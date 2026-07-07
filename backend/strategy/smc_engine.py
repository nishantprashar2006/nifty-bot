"""
strategy/smc_engine.py
======================
Smart Money Concepts engine — v1.5 (PART 2 spec).

Strictly deterministic. No AI, no probability models, no discretion.
Given identical bars, always emits identical SMCResult.

Concepts implemented (and ONLY these):
    • Market Structure (HH/HL, LH/LL)
    • Break of Structure (BOS)        — close-confirmed
    • Change of Character (CHoCH)     — first structural reversal only
    • Liquidity Sweeps                — wick beyond level + close back
    • Equal Highs / Equal Lows (EQH/EQL)
    • Fair Value Gaps (FVG)           — 3-candle imbalance
    • Order Blocks                    — last opposite candle before
                                        confirmed DISPLACEMENT
    • Displacement                    — body > ATR × 1.5 AND close at extreme
    • Premium / Discount              — last impulse leg
    • Market Regime                   — Trending / Sideways / High-Vol /
                                        Low-Vol; reduces confidence, never
                                        suppresses signals
    • HTF Trend                       — derived from 15m STRUCTURE (HH/HL
                                        or LH/LL), NOT from indicators

Confidence weighting (max 100):
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

# ───────────────────────────── tunables ────────────────────────────────
# Default fractal width — bar must be the strict extreme of ±SWING_WINDOW
SWING_WINDOW: int = 5

# Two swings count as "equal" when within EQ_TOLERANCE_BPS basis points
# (0.05% = 5 bps).
EQ_TOLERANCE_BPS: float = 5.0

# Displacement: real body must exceed ATR × DISPLACEMENT_ATR_MULT and the
# close must be within DISPLACEMENT_CLOSE_PCT of the candle's extreme.
ATR_PERIOD: int = 14
DISPLACEMENT_ATR_MULT: float = 1.5
DISPLACEMENT_CLOSE_PCT: float = 0.30   # close in top/bottom 30 % of range

# Order-block scanner only looks at the most recent OB_LOOKBACK bars.
OB_LOOKBACK: int = 40
FVG_LOOKBACK: int = 40

# Regime classifier thresholds (applied to ATR / price)
# Regime classifier thresholds (applied to ATR / price)
REGIME_HIGH_VOL_PCT: float = 0.008   # ATR > 0.8 % of price  → high vol
REGIME_LOW_VOL_PCT: float = 0.002    # ATR < 0.2 % of price  → low vol

# Widen the "recent event" window for BOS/CHoCH and Liquidity Sweeps — an
# event that fired on the previous bar is still a valid setup on the
# current bar. Prevents genuine signals from evaporating after a single
# 5-minute candle roll. 3 bars ≈ 15 minutes on the 5m execution series.
RECENT_EVENT_BARS: int = 3

# Confidence multipliers per regime (regime never suppresses, only attenuates)
REGIME_CONF_MULT: dict[str, float] = {
    "TRENDING":  1.00,
    "SIDEWAYS":  0.80,
    "HIGH_VOL":  0.85,
    "LOW_VOL":   0.90,
    "UNCLEAR":   0.85,
}


Direction = Literal["CALL", "PUT", "NEUTRAL"]
Regime = Literal["TRENDING", "SIDEWAYS", "HIGH_VOL", "LOW_VOL", "UNCLEAR"]


# ───────────────────────────── primitives ──────────────────────────────
@dataclass(frozen=True)
class OrderBlock:
    side: Literal["BULL", "BEAR"]
    high: float
    low: float
    idx: int          # index of the OB candle in the source bar list
    mitigated: bool   # touched (price entered the zone) after creation
    broken: bool      # closed beyond the far side after creation


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
    structure: Direction = "NEUTRAL"              # HH/HL = CALL, LH/LL = PUT
    bos: Optional[Direction] = None
    choch: Optional[Direction] = None
    sweep: Optional[Direction] = None             # direction the sweep favours
    ob_retest: Optional[OrderBlock] = None
    fvg_mitigation: Optional[FVG] = None
    premium_discount: Literal["PREMIUM", "DISCOUNT", "MID"] = "MID"
    eqh: bool = False
    eql: bool = False
    last_close: Optional[float] = None
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    atr: Optional[float] = None
    regime: Regime = "UNCLEAR"
    regime_mult: float = 1.0


@dataclass
class SMCResult:
    direction: Direction
    confidence: int
    reasons: list[str] = field(default_factory=list)
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    ctx: Optional[SMCContext] = None


# ──────────────────────────── helpers ──────────────────────────────────
def _wilder_atr(bars: list[Bar], period: int = ATR_PERIOD) -> Optional[float]:
    """Wilder-smoothed ATR. Returns None until `period+1` bars available."""
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        h = bars[i].high
        l = bars[i].low
        cp = bars[i - 1].close
        trs.append(max(h - l, abs(h - cp), abs(l - cp)))
    # initial average then Wilder smoothing
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ──────────────────────────── detectors ────────────────────────────────
def detect_displacement(bar: Bar, atr: float) -> Optional[Literal["BULL", "BEAR"]]:
    """A displacement candle = strong directional intent. Spec:
        • |body| > atr × DISPLACEMENT_ATR_MULT
        • close near the candle's extreme (top/bottom DISPLACEMENT_CLOSE_PCT)
    """
    if atr is None or atr <= 0:
        return None
    body = abs(bar.close - bar.open)
    rng = bar.high - bar.low
    if rng <= 0 or body <= atr * DISPLACEMENT_ATR_MULT:
        return None
    # close-near-extreme check
    if bar.close > bar.open:
        # bullish — close should be in the upper DISPLACEMENT_CLOSE_PCT of range
        if (bar.high - bar.close) <= rng * DISPLACEMENT_CLOSE_PCT:
            return "BULL"
    else:
        if (bar.close - bar.low) <= rng * DISPLACEMENT_CLOSE_PCT:
            return "BEAR"
    return None


def detect_htf_trend(bars_15m: list[Bar]) -> Direction:
    """Spec PART 2: HTF trend from STRUCTURE only (HH/HL vs LH/LL on 15m
    swings). Never from EMAs/indicators."""
    swings = find_swings(bars_15m, lookback=SWING_WINDOW)
    return detect_structure(swings)


def detect_structure(swings: list[Swing]) -> Direction:
    """Structure-based bias.

    Prefers the **last 3 confirmed swings on each side** using endpoint
    comparison — `highs[-1]` vs `highs[-3]` and `lows[-1]` vs `lows[-3]`.
    Endpoint comparison ignores a single anomalous middle swing (e.g. a
    noisy pullback low) that used to force NEUTRAL under the strict
    last-2 rule.

    Falls back to the original strict last-2 comparison when only 2
    confirmed swings exist (early session warm-up), so no early-session
    behaviour is lost.

    Fully deterministic — no tolerances, EMAs, ATR, or new primitives.
    HH+HL → CALL ·  LH+LL → PUT ·  otherwise NEUTRAL.
    """
    highs = [s for s in swings if s.side == "HIGH"]
    lows = [s for s in swings if s.side == "LOW"]

    if len(highs) >= 3 and len(lows) >= 3:
        # Endpoint check across the last 3 confirmed swings per side —
        # one anomalous middle swing no longer flips the verdict.
        hh = highs[-1].price > highs[-3].price
        hl = lows[-1].price > lows[-3].price
        lh = highs[-1].price < highs[-3].price
        ll = lows[-1].price < lows[-3].price
    elif len(highs) >= 2 and len(lows) >= 2:
        # Warm-up fallback — original strict last-2 rule.
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


def detect_bos_choch(
    swings: list[Swing], bars: list[Bar],
) -> tuple[Optional[Direction], Optional[Direction], Direction]:
    """Sequentially walk through swing levels and bars to track structure.
    Returns (bos, choch, current_trend) where bos/choch are the latest events
    (if any happened on the most recent bar) and current_trend is the running
    trend after processing every confirmed break.

    CHoCH is emitted only on the FIRST opposite-direction break. Any further
    breaks in the new direction are BOS.
    """
    if not bars or not swings:
        return None, None, "NEUTRAL"

    # Build a time-ordered queue of swing levels.
    # We "consume" a swing high when a bar closes above it (bullish break);
    # a swing low when a bar closes below it (bearish break).
    trend: Direction = "NEUTRAL"
    last_bos: Optional[Direction] = None
    last_choch: Optional[Direction] = None

    # Snapshot of the most recent bar index where an event fired
    last_event_idx = -1

    active_highs: list[Swing] = [s for s in swings if s.side == "HIGH"]
    active_lows: list[Swing] = [s for s in swings if s.side == "LOW"]

    for i, b in enumerate(bars):
        # iterate copies to allow removal
        broke_up = False
        broke_dn = False
        # bullish break — closes above any active swing high whose bar < i
        for s in list(active_highs):
            if s.idx >= i:
                continue
            if b.close > s.price:
                broke_up = True
                active_highs.remove(s)
        for s in list(active_lows):
            if s.idx >= i:
                continue
            if b.close < s.price:
                broke_dn = True
                active_lows.remove(s)

        if broke_up and not broke_dn:
            if trend == "PUT":          # first reversal up
                last_choch = "CALL"
                trend = "CALL"
            else:                       # continuation or initial
                last_bos = "CALL"
                if trend == "NEUTRAL":
                    trend = "CALL"
            last_event_idx = i
        elif broke_dn and not broke_up:
            if trend == "CALL":
                last_choch = "PUT"
                trend = "PUT"
            else:
                last_bos = "PUT"
                if trend == "NEUTRAL":
                    trend = "PUT"
            last_event_idx = i
        elif broke_up and broke_dn:
            # ambiguous bar — keep the trend, no event emitted
            pass

    # Surface bos/choch if they fired within the last RECENT_EVENT_BARS —
    # a genuine BOS or CHoCH remains actionable for ~15 min after it forms.
    if last_event_idx < len(bars) - RECENT_EVENT_BARS:
        return None, None, trend
    return last_bos, last_choch, trend


def detect_liquidity_sweep(bars: list[Bar], swings: list[Swing]) -> Optional[Direction]:
    """Wick beyond a recent swing then close back inside it (same candle).

    Scans the last RECENT_EVENT_BARS candles instead of only the current one —
    a sweep that printed 5-10 minutes ago is still a valid setup context.
    Returns the direction of the most recent sweep, if any.
    """
    if not bars or len(swings) < 1:
        return None
    recent_highs = [s for s in swings[-8:] if s.side == "HIGH"]
    recent_lows = [s for s in swings[-8:] if s.side == "LOW"]
    # Walk the last N bars newest-first so we surface the freshest sweep
    for candle in reversed(bars[-RECENT_EVENT_BARS:]):
        for s in recent_highs:
            if candle.high > s.price and candle.close < s.price:
                return "PUT"   # swept buy-side liquidity → bearish
        for s in recent_lows:
            if candle.low < s.price and candle.close > s.price:
                return "CALL"  # swept sell-side liquidity → bullish
    return None


def detect_order_blocks(bars: list[Bar], atr: Optional[float]) -> list[OrderBlock]:
    """Spec PART 2: OB = last OPPOSITE-color candle immediately before a
    confirmed displacement candle. Tracks `mitigated` (price re-entered the
    zone) and `broken` (close beyond the far side)."""
    out: list[OrderBlock] = []
    if atr is None or len(bars) < 3:
        return out
    start = max(1, len(bars) - OB_LOOKBACK)
    for i in range(start, len(bars)):
        disp = detect_displacement(bars[i], atr)
        if disp is None:
            continue
        # Walk back to the most recent opposite-color candle
        for j in range(i - 1, max(-1, i - 6), -1):
            prev = bars[j]
            if disp == "BULL" and prev.close < prev.open:
                ob_side = "BULL"
                break
            if disp == "BEAR" and prev.close > prev.open:
                ob_side = "BEAR"
                break
        else:
            continue
        ob_high = bars[j].high
        ob_low = bars[j].low
        # Mitigation: any later bar touched the zone
        mitigated = any(
            bars[k].low <= ob_high and bars[k].high >= ob_low
            for k in range(i + 1, len(bars))
        )
        # Broken: a later close went past the far side of the zone
        if ob_side == "BULL":
            broken = any(bars[k].close < ob_low for k in range(i + 1, len(bars)))
        else:
            broken = any(bars[k].close > ob_high for k in range(i + 1, len(bars)))
        out.append(OrderBlock(ob_side, ob_high, ob_low, j, mitigated, broken))
    return out


def detect_fvgs(bars: list[Bar]) -> list[FVG]:
    """Standard 3-candle FVG: bull if bar[i+2].low > bar[i].high; bear mirror."""
    out: list[FVG] = []
    start = max(0, len(bars) - FVG_LOOKBACK)
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
    """Latest still-valid (unbroken) OB whose zone has been touched by the
    current bar. Mitigated OBs that are not broken are still valid for retest."""
    if not bars or not obs:
        return None
    last = bars[-1]
    for ob in reversed(obs):
        if ob.broken:
            continue
        if ob.low <= last.low <= ob.high or ob.low <= last.high <= ob.high:
            return ob
    return None


def detect_eq_levels(swings: list[Swing], tol_bps: float = EQ_TOLERANCE_BPS) -> tuple[bool, bool]:
    """EQH/EQL — two same-side swings within `tol_bps` basis points."""
    highs = [s for s in swings if s.side == "HIGH"][-3:]
    lows = [s for s in swings if s.side == "LOW"][-3:]

    def near(a: float, b: float) -> bool:
        return abs(a - b) / max(a, 1) * 10_000 <= tol_bps

    eqh = len(highs) >= 2 and near(highs[-1].price, highs[-2].price)
    eql = len(lows) >= 2 and near(lows[-1].price, lows[-2].price)
    return eqh, eql


def detect_premium_discount(bars: list[Bar], swings: list[Swing]) -> tuple[str, float, float]:
    """Premium/discount within the most recent impulse leg."""
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


def detect_regime(bars: list[Bar], swings: list[Swing], atr: Optional[float]) -> Regime:
    """Classify market regime via ATR-as-%-of-price + swing progression."""
    if not bars or atr is None:
        return "UNCLEAR"
    price = bars[-1].close
    atr_pct = atr / price if price > 0 else 0.0
    if atr_pct >= REGIME_HIGH_VOL_PCT:
        return "HIGH_VOL"
    if atr_pct <= REGIME_LOW_VOL_PCT:
        return "LOW_VOL"
    # ATR in normal band → look at swing progression
    highs = [s.price for s in swings if s.side == "HIGH"][-3:]
    lows = [s.price for s in swings if s.side == "LOW"][-3:]
    if len(highs) >= 2 and len(lows) >= 2:
        progressing_up = highs[-1] > highs[-2] and lows[-1] > lows[-2]
        progressing_dn = highs[-1] < highs[-2] and lows[-1] < lows[-2]
        if progressing_up or progressing_dn:
            return "TRENDING"
    return "SIDEWAYS"


# ────────────────────────── main scoring engine ─────────────────────────
def evaluate(bars_5m: list[Bar], bars_15m: list[Bar]) -> SMCResult:
    """Run all detectors, score, and emit a signal. Pure function — given
    the same inputs it always produces the same output."""
    if len(bars_5m) < 2 * SWING_WINDOW + 5 or len(bars_15m) < 2 * SWING_WINDOW + 1:
        return SMCResult("NEUTRAL", 0, ["warming_up"], None, None, None, SMCContext())

    swings_5m = find_swings(bars_5m, lookback=SWING_WINDOW)
    atr_5m = _wilder_atr(bars_5m, period=ATR_PERIOD)

    ctx = SMCContext()
    ctx.last_close = bars_5m[-1].close
    ctx.atr = atr_5m
    ctx.htf_trend = detect_htf_trend(bars_15m)
    ctx.structure = detect_structure(swings_5m)
    ctx.bos, ctx.choch, _running_trend = detect_bos_choch(swings_5m, bars_5m)
    ctx.sweep = detect_liquidity_sweep(bars_5m, swings_5m)
    obs = detect_order_blocks(bars_5m, atr_5m)
    ctx.ob_retest = detect_ob_retest(bars_5m, obs)
    fvgs = detect_fvgs(bars_5m)
    unmitigated = [f for f in fvgs if not f.mitigated]
    ctx.fvg_mitigation = unmitigated[-1] if unmitigated else None
    ctx.eqh, ctx.eql = detect_eq_levels(swings_5m)
    zone, rng_hi, rng_lo = detect_premium_discount(bars_5m, swings_5m)
    ctx.premium_discount = zone  # type: ignore[assignment]
    ctx.range_high, ctx.range_low = rng_hi, rng_lo
    ctx.regime = detect_regime(bars_5m, swings_5m, atr_5m)
    ctx.regime_mult = REGIME_CONF_MULT.get(ctx.regime, 1.0)

    # Score each side independently using the user's weights
    call_score = 0
    put_score = 0
    reasons_call: list[str] = []
    reasons_put: list[str] = []

    if ctx.htf_trend == "CALL":
        call_score += 20; reasons_call.append("HTF trend bullish HH+HL (+20)")
    elif ctx.htf_trend == "PUT":
        put_score += 20; reasons_put.append("HTF trend bearish LH+LL (+20)")

    if ctx.structure == "CALL":
        call_score += 15; reasons_call.append("5m structure HH+HL (+15)")
    elif ctx.structure == "PUT":
        put_score += 15; reasons_put.append("5m structure LH+LL (+15)")

    if ctx.choch == "CALL":
        call_score += 20; reasons_call.append("CHoCH bullish — first reversal (+20)")
    elif ctx.bos == "CALL":
        call_score += 20; reasons_call.append("BOS bullish — close-confirmed (+20)")
    if ctx.choch == "PUT":
        put_score += 20; reasons_put.append("CHoCH bearish — first reversal (+20)")
    elif ctx.bos == "PUT":
        put_score += 20; reasons_put.append("BOS bearish — close-confirmed (+20)")

    if ctx.sweep == "CALL":
        call_score += 15
        sweep_label = "EQL sweep" if ctx.eql else "sell-side liquidity sweep"
        reasons_call.append(f"{sweep_label} (+15)")
    elif ctx.sweep == "PUT":
        put_score += 15
        sweep_label = "EQH sweep" if ctx.eqh else "buy-side liquidity sweep"
        reasons_put.append(f"{sweep_label} (+15)")

    if ctx.ob_retest:
        tag = "mitigated " if ctx.ob_retest.mitigated else ""
        if ctx.ob_retest.side == "BULL":
            call_score += 15; reasons_call.append(f"Bull OB {tag}retest (+15)")
        else:
            put_score += 15; reasons_put.append(f"Bear OB {tag}retest (+15)")

    if ctx.fvg_mitigation:
        if ctx.fvg_mitigation.side == "BULL":
            call_score += 10; reasons_call.append("Bull FVG mitigation (+10)")
        else:
            put_score += 10; reasons_put.append("Bear FVG mitigation (+10)")

    # Premium/discount — buy in discount, sell in premium
    if ctx.premium_discount == "DISCOUNT":
        call_score += 5; reasons_call.append("In discount zone (+5)")
    elif ctx.premium_discount == "PREMIUM":
        put_score += 5; reasons_put.append("In premium zone (+5)")

    # Regime attenuation — never suppress, only reduce confidence
    call_score = round(call_score * ctx.regime_mult)
    put_score = round(put_score * ctx.regime_mult)
    if ctx.regime_mult < 1.0:
        regime_note = f"Regime {ctx.regime} — confidence × {ctx.regime_mult:.2f}"
        reasons_call.append(regime_note)
        reasons_put.append(regime_note)

    # Pick the dominant side; ties → NEUTRAL
    if call_score == put_score:
        return SMCResult("NEUTRAL", max(call_score, put_score),
                         reasons_call + reasons_put, None, None, None, ctx)

    direction: Direction = "CALL" if call_score > put_score else "PUT"
    confidence = max(call_score, put_score)
    reasons = reasons_call if direction == "CALL" else reasons_put

    # Entry / SL / TP — anchored to last close & impulse leg
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
    """Heat-map bands kept for backward compatibility with the dashboard."""
    if confidence >= 80: return "STRONG"
    if confidence >= 60: return "GOOD"
    if confidence >= 40: return "NEUTRAL"
    if confidence >= 20: return "WEAK"
    return "AVOID"
