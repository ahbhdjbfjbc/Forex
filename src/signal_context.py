"""Analysis context shown with every recommendation (text and chart).

Computed once from the same OHLCV frame the signal was generated from, so the
caption and the image cannot disagree. Only closed, prior data is used for
support/resistance; structure comes from src.market_structure (no look-ahead).
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd

from src.indicators import atr as atr_series
from src.market_structure import MarketStructure, PriceZone, analyze_structure, direction_word
from src.models import TradeDirection, TradeSignal
from src.price_format import round_price

ENTRY_ZONE_ATR = 0.10

_TREND_AR = {"BULLISH": "صاعد", "BEARISH": "هابط", "NEUTRAL": "غير محدد"}


@dataclass(frozen=True)
class SignalContext:
    support: float
    resistance: float
    entry_low: float
    entry_high: float
    atr: float | None
    structure: MarketStructure | None
    zones: tuple[PriceZone, ...]  # FVG / order blocks that agree with the trade

    def structure_text(self) -> str | None:
        if self.structure is None:
            return None
        text = f"الهيكل: {_TREND_AR.get(self.structure.trend, self.structure.trend)}"
        event = self.structure.last_event
        if event is not None:
            text += f" | آخر كسر {event.kind} {_TREND_AR.get(event.direction, event.direction)} قبل {event.bars_ago} شمعة"
        return text


def build_signal_context(signal: TradeSignal, frame: pd.DataFrame | None) -> SignalContext:
    entry = float(signal.entry_price)
    symbol = signal.symbol
    atr_value = None
    structure = None
    if frame is not None and len(frame) > 15 and {"High", "Low", "Close"}.issubset(frame.columns):
        try:
            value = float(atr_series(frame.astype(float), 14).iloc[-1])
            atr_value = value if math.isfinite(value) and value > 0 else None
        except Exception:
            atr_value = None
        if atr_value is not None and "Open" in frame.columns:
            try:
                structure = analyze_structure(frame.astype(float), atr_value)
            except Exception:
                structure = None

    support = resistance = None
    if structure is not None:
        if structure.support_zone is not None:
            support = (structure.support_zone.low + structure.support_zone.high) / 2
        if structure.resistance_zone is not None:
            resistance = (structure.resistance_zone.low + structure.resistance_zone.high) / 2
    if frame is not None and len(frame) > 1 and (support is None or resistance is None):
        prior = frame.iloc[:-1].tail(50)
        if not prior.empty:
            support = float(prior["Low"].min()) if support is None else support
            resistance = float(prior["High"].max()) if resistance is None else resistance
    support = entry if support is None else support
    resistance = entry if resistance is None else resistance

    half = ENTRY_ZONE_ATR * atr_value if atr_value else 0.0
    zones: list[PriceZone] = []
    if structure is not None:
        wanted = direction_word(signal.direction.value if isinstance(signal.direction, TradeDirection) else str(signal.direction))
        zones.extend(z for z in structure.fair_value_gaps if z.direction == wanted)
        if structure.order_block is not None and structure.order_block.direction == wanted:
            zones.append(structure.order_block)
    return SignalContext(
        support=round_price(support, symbol),
        resistance=round_price(resistance, symbol),
        entry_low=round_price(entry - half, symbol),
        entry_high=round_price(entry + half, symbol),
        atr=atr_value,
        structure=structure,
        zones=tuple(zones[:3]),
    )
