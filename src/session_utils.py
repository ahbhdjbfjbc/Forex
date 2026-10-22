"""Forex trading-session detection and relative-volume (RVOL) calculation.

Session boundaries are the conventional UTC approximations used across the
retail FX industry (they ignore daylight-saving shifts by design — good
enough for regime classification, not for precise session-open timestamps).
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src.models import ForexSession

_ASIAN_START, _ASIAN_END = 0, 8
_LONDON_START, _LONDON_END = 8, 17
_NY_START, _NY_END = 13, 22
_OVERLAP_START, _OVERLAP_END = 13, 17


def get_forex_session(dt: datetime | None = None) -> ForexSession:
    dt = (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if dt.weekday() >= 5:  # Saturday/Sunday — market effectively closed
        return ForexSession.OFF_HOURS

    hour = dt.hour
    if _OVERLAP_START <= hour < _OVERLAP_END:
        return ForexSession.LONDON_NY_OVERLAP
    if _LONDON_START <= hour < _LONDON_END:
        return ForexSession.LONDON
    if _NY_START <= hour < _NY_END:
        return ForexSession.NEW_YORK
    if _ASIAN_START <= hour < _ASIAN_END:
        return ForexSession.ASIAN
    return ForexSession.OFF_HOURS


def compute_rvol(volume: pd.Series, lookback: int = 20) -> float:
    """Latest bar volume relative to the average of the `lookback` bars
    immediately preceding it (the baseline deliberately excludes the latest
    bar itself — otherwise a volume collapse drags its own baseline down
    with it and the ratio stays near 1.0 no matter how illiquid things get).

    Many retail FX feeds (including yfinance FX tickers) report tick volume,
    or all-zero volume for some pairs/timeframes. When the trailing average
    is zero or NaN, we can't measure liquidity from volume at all — return
    1.0 (neutral) rather than dividing by zero or fabricating a signal.
    """
    clean = volume.dropna()
    if len(clean) < max(5, lookback // 4) + 1:
        return 1.0
    latest = clean.iloc[-1]
    baseline_window = clean.iloc[-(lookback + 1) : -1]
    avg = baseline_window.mean()
    if not np.isfinite(avg) or avg <= 0:
        return 1.0
    return float(latest / avg)
