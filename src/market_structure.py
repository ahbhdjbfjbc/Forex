"""Price-action market structure computed only from the OHLC bars supplied.

Everything here is a deterministic function of the candles up to and
including the latest bar, so the same code is safe for live scans and for
walk-forward backtests (no future bar is ever consulted):

* swing highs/lows  — fractal pivots, only "known" once `swing_window`
  bars have closed after the pivot (no repainting/look-ahead);
* BOS / CHOCH       — a close beyond the latest known swing. A break in the
  direction of the prevailing structure is a Break Of Structure; a break
  against it is a Change Of Character;
* Fair Value Gaps   — three-candle imbalances still unfilled at the latest bar;
* Order Blocks      — the last opposite-colour candle before the impulse
  that produced the most recent BOS/CHOCH (a demand/supply origin zone);
* liquidity sweeps  — the latest bar trades through a known swing and closes
  back inside it (stop-run of resting liquidity);
* support/resistance zones — nearest known swing low below / swing high above
  price, widened by a fraction of ATR.

These are rule-based descriptions of OHLC data. They are not order-flow or
broker depth data, and they are not a success probability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import pandas as pd

BULLISH = "BULLISH"
BEARISH = "BEARISH"
NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class StructureEvent:
    kind: str          # "BOS" or "CHOCH"
    direction: str     # BULLISH / BEARISH
    level: float       # broken swing level
    bar_index: int     # position (0-based) in the analysed frame
    bars_ago: int


@dataclass(frozen=True)
class PriceZone:
    kind: str          # "FVG", "ORDER_BLOCK", "SUPPORT", "RESISTANCE"
    direction: str     # BULLISH (demand) / BEARISH (supply)
    low: float
    high: float
    start_index: int

    def contains(self, price: float, tolerance: float = 0.0) -> bool:
        return self.low - tolerance <= price <= self.high + tolerance


@dataclass(frozen=True)
class LiquiditySweep:
    direction: str     # BULLISH = sell-side liquidity swept below a low; BEARISH = buy-side above a high
    level: float
    bar_index: int


@dataclass(frozen=True)
class MarketStructure:
    trend: str
    last_event: StructureEvent | None
    events: tuple[StructureEvent, ...]
    fair_value_gaps: tuple[PriceZone, ...]
    order_block: PriceZone | None
    sweep: LiquiditySweep | None
    support_zone: PriceZone | None
    resistance_zone: PriceZone | None
    swing_highs: tuple[tuple[int, float], ...] = field(default_factory=tuple)
    swing_lows: tuple[tuple[int, float], ...] = field(default_factory=tuple)
    offset: int = 0  # index of the first analysed bar inside the caller's full frame


def _finite(*values: float) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in values)


def analyze_structure(
    df: pd.DataFrame,
    atr: float,
    *,
    swing_window: int = 3,
    lookback: int = 160,
    fvg_min_atr: float = 0.10,
) -> MarketStructure | None:
    """Return the current structure, or None when the frame is unusable."""
    required = {"Open", "High", "Low", "Close"}
    if df is None or not required.issubset(df.columns) or len(df) < swing_window * 2 + 10:
        return None
    if not _finite(atr) or atr <= 0:
        return None
    frame = df.tail(lookback)
    offset = len(df) - len(frame)
    opens = frame["Open"].astype(float).to_numpy()
    highs = frame["High"].astype(float).to_numpy()
    lows = frame["Low"].astype(float).to_numpy()
    closes = frame["Close"].astype(float).to_numpy()
    n = len(frame)
    if not all(map(math.isfinite, (*highs, *lows, *closes, *opens))):
        return None

    pivot_highs: list[tuple[int, float]] = []
    pivot_lows: list[tuple[int, float]] = []
    for i in range(swing_window, n - swing_window):
        window = slice(i - swing_window, i + swing_window + 1)
        if highs[i] == highs[window].max():
            pivot_highs.append((i, float(highs[i])))
        if lows[i] == lows[window].min():
            pivot_lows.append((i, float(lows[i])))

    # Walk forward bar by bar; a pivot at i becomes known at bar i + swing_window.
    trend = NEUTRAL
    events: list[StructureEvent] = []
    known_high: tuple[int, float] | None = None
    known_low: tuple[int, float] | None = None
    high_broken = low_broken = False
    ph = pl = 0
    for j in range(n):
        while ph < len(pivot_highs) and pivot_highs[ph][0] + swing_window <= j:
            known_high = pivot_highs[ph]
            high_broken = False
            ph += 1
        while pl < len(pivot_lows) and pivot_lows[pl][0] + swing_window <= j:
            known_low = pivot_lows[pl]
            low_broken = False
            pl += 1
        if known_high is not None and not high_broken and closes[j] > known_high[1]:
            kind = "CHOCH" if trend == BEARISH else "BOS"
            events.append(StructureEvent(kind, BULLISH, known_high[1], j, n - 1 - j))
            trend = BULLISH
            high_broken = True
        elif known_low is not None and not low_broken and closes[j] < known_low[1]:
            kind = "CHOCH" if trend == BULLISH else "BOS"
            events.append(StructureEvent(kind, BEARISH, known_low[1], j, n - 1 - j))
            trend = BEARISH
            low_broken = True

    # Unfilled fair value gaps (3-candle imbalance), newest first, max 3.
    gaps: list[PriceZone] = []
    min_gap = fvg_min_atr * atr
    for i in range(2, n):
        if lows[i] > highs[i - 2] and lows[i] - highs[i - 2] >= min_gap:
            zone_low, zone_high = float(highs[i - 2]), float(lows[i])
            later_low = lows[i + 1:].min() if i + 1 < n else math.inf
            if later_low > zone_low:
                gaps.append(PriceZone("FVG", BULLISH, zone_low, zone_high, i - 2))
        elif highs[i] < lows[i - 2] and lows[i - 2] - highs[i] >= min_gap:
            zone_low, zone_high = float(highs[i]), float(lows[i - 2])
            later_high = highs[i + 1:].max() if i + 1 < n else -math.inf
            if later_high < zone_high:
                gaps.append(PriceZone("FVG", BEARISH, zone_low, zone_high, i - 2))
    gaps = sorted(gaps, key=lambda z: z.start_index, reverse=True)[:3]

    # Order block: last opposite candle before the impulse of the latest break.
    order_block = None
    last_event = events[-1] if events else None
    if last_event is not None:
        start = max(0, last_event.bar_index - 12)
        for k in range(last_event.bar_index, start - 1, -1):
            bearish_candle = closes[k] < opens[k]
            bullish_candle = closes[k] > opens[k]
            if last_event.direction == BULLISH and bearish_candle:
                order_block = PriceZone("ORDER_BLOCK", BULLISH, float(lows[k]), float(highs[k]), k)
                break
            if last_event.direction == BEARISH and bullish_candle:
                order_block = PriceZone("ORDER_BLOCK", BEARISH, float(lows[k]), float(highs[k]), k)
                break

    # Liquidity sweep on the latest bar against the latest known swings.
    sweep = None
    last = n - 1
    prior_highs = [p for p in pivot_highs if p[0] + swing_window <= last - 1]
    prior_lows = [p for p in pivot_lows if p[0] + swing_window <= last - 1]
    if prior_highs:
        level = prior_highs[-1][1]
        if highs[last] > level and closes[last] < level:
            sweep = LiquiditySweep(BEARISH, level, last)
    if sweep is None and prior_lows:
        level = prior_lows[-1][1]
        if lows[last] < level and closes[last] > level:
            sweep = LiquiditySweep(BULLISH, level, last)

    price = float(closes[last])
    pad = 0.25 * atr
    known_lows = [p for p in pivot_lows if p[0] + swing_window <= last]
    known_highs = [p for p in pivot_highs if p[0] + swing_window <= last]
    below = [p for p in known_lows if p[1] < price]
    above = [p for p in known_highs if p[1] > price]
    support_zone = None
    resistance_zone = None
    if below:
        idx, level = max(below, key=lambda p: p[1])
        support_zone = PriceZone("SUPPORT", BULLISH, level - pad, level + pad, idx)
    if above:
        idx, level = min(above, key=lambda p: p[1])
        resistance_zone = PriceZone("RESISTANCE", BEARISH, level - pad, level + pad, idx)

    return MarketStructure(
        trend=trend,
        last_event=last_event,
        events=tuple(events[-5:]),
        fair_value_gaps=tuple(gaps),
        order_block=order_block,
        sweep=sweep,
        support_zone=support_zone,
        resistance_zone=resistance_zone,
        swing_highs=tuple(known_highs[-6:]),
        swing_lows=tuple(known_lows[-6:]),
        offset=offset,
    )


def direction_word(direction: str) -> str:
    return BULLISH if str(direction).upper() == "BUY" else BEARISH


def structure_conflict(
    structure: MarketStructure | None,
    direction: str,
    *,
    counter_trend_strategy: bool = False,
    recent_bars: int = 20,
) -> str | None:
    """Return a rejection reason when recent structure opposes the trade."""
    if structure is None or counter_trend_strategy:
        # Mean-reversion fades trade against recent structure by design.
        return None
    wanted = direction_word(direction)
    opposite = BEARISH if wanted == BULLISH else BULLISH
    if structure.sweep is not None and structure.sweep.direction == opposite:
        return "سحب سيولة معاكس على آخر شمعة"
    event = structure.last_event
    if (
        event is not None
        and event.direction == opposite
        and event.bars_ago <= recent_bars
    ):
        return f"آخر كسر هيكلي {event.kind} معاكس للصفقة قبل {event.bars_ago} شمعة"
    return None


def structure_confluence(
    structure: MarketStructure | None, direction: str, entry: float, atr: float
) -> tuple[str, ...]:
    """Observed price-action confirmations that agree with the trade direction."""
    if structure is None or not _finite(entry, atr) or atr <= 0:
        return ()
    wanted = direction_word(direction)
    reasons: list[str] = []
    if structure.last_event is not None and structure.last_event.direction == wanted:
        reasons.append(f"{structure.last_event.kind} في اتجاه الصفقة")
    tolerance = 0.5 * atr
    for gap in structure.fair_value_gaps:
        if gap.direction == wanted and gap.contains(entry, tolerance):
            reasons.append("الدخول قرب فجوة FVG داعمة")
            break
    block = structure.order_block
    if block is not None and block.direction == wanted and block.contains(entry, tolerance):
        reasons.append("الدخول قرب Order Block داعم")
    if structure.sweep is not None and structure.sweep.direction == wanted:
        reasons.append("سحب سيولة في اتجاه الصفقة")
    return tuple(reasons)
