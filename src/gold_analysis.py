"""Gold-specific market context derived only from the OHLCV frame in use.

This module intentionally reports measurable context (ATR, recent range,
prior-bar support/resistance and range break) rather than inventing win rates
or claiming order-flow data that Yahoo Finance does not provide. XAUUSD may be
backed by Yahoo's COMEX gold-futures proxy (GC=F), not spot XAUUSD; callers
must preserve that source caveat in operator-facing documentation.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class GoldMarketContext:
    close: float
    atr: float
    atr_percent: float
    support: float
    resistance: float
    prior_range_high: float
    prior_range_low: float
    close_breaks_range_high: bool
    close_breaks_range_low: bool
    current_range_atr: float


def analyze_gold_market(df: pd.DataFrame, atr: float, lookback: int = 50) -> GoldMarketContext | None:
    """Summarize recent XAU-like price structure; no future bars are consulted."""
    required = {"High", "Low", "Close"}
    if not required.issubset(df.columns) or len(df) < max(lookback, 22):
        return None
    if not np.isfinite(float(atr)) or atr <= 0:
        return None
    recent = df.tail(lookback)
    prior = recent.iloc[:-1]
    close = float(recent["Close"].iloc[-1])
    high = float(recent["High"].iloc[-1])
    low = float(recent["Low"].iloc[-1])
    prior_high = float(prior["High"].tail(20).max())
    prior_low = float(prior["Low"].tail(20).min())
    if not all(np.isfinite(value) and value > 0 for value in (close, high, low, prior_high, prior_low)):
        return None
    return GoldMarketContext(
        close=close,
        atr=float(atr),
        atr_percent=float(atr / close * 100.0),
        support=float(prior["Low"].min()),
        resistance=float(prior["High"].max()),
        prior_range_high=prior_high,
        prior_range_low=prior_low,
        close_breaks_range_high=close > prior_high,
        close_breaks_range_low=close < prior_low,
        current_range_atr=float((high - low) / atr),
    )


def validate_gold_entry(
    context: GoldMarketContext | None,
    direction: str,
    entry: float,
    take_profit: float,
) -> tuple[bool, str]:
    """Reject an entry when a nearby opposing 20-bar boundary blocks TP1.

    A confirmed close beyond the same boundary is treated as a range break,
    not as an obstacle. This is a structural filter, not a success probability.
    """
    if context is None:
        return False, "بيانات هيكل الذهب غير كافية أو ATR غير صالح"
    direction = str(direction).upper()
    if direction == "BUY":
        if context.resistance > entry and entry < context.resistance < take_profit:
            if not context.close_breaks_range_high:
                return False, "مقاومة النطاق السابق تقع قبل الهدف الأول"
    elif direction == "SELL":
        if context.support < entry and take_profit < context.support < entry:
            if not context.close_breaks_range_low:
                return False, "دعم النطاق السابق يقع قبل الهدف الأول"
    else:
        return False, "اتجاه إشارة الذهب غير صالح"
    return True, "تم اجتياز فحص ATR ومستويات النطاق السابقة"
