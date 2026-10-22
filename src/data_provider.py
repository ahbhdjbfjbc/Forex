"""OHLCV data acquisition.

`yfinance` is the primary source because it returns full historical bars,
which `indicators.py` needs to compute ADX/EMA/ATR/BB over a lookback window.
`tradingview_ta` is offered as a lightweight secondary/confirmation source —
it only exposes a single current-bar analysis snapshot (no history), so it
cannot replace yfinance as the primary feed, but it's useful to sanity-check
a regime call against TradingView's own server-side indicator values.

Every public function fails soft: on network errors, rate limits, or missing
data it logs and returns `None` (or an empty frame) rather than raising, so
one bad symbol never kills a full watchlist scan.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

# yfinance has no native "4h" bar; we pull hourly bars and resample.
_BASE_INTERVAL = {
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "1h",
    "1d": "1d",
}
_MAX_PERIOD = {
    "15m": "60d",
    "30m": "60d",
    "1h": "730d",
    "4h": "730d",
    "1d": "5y",
}
_RESAMPLE_RULE = {"4h": "4h"}

REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


class DataFetchError(Exception):
    """Raised internally, always caught at the public-function boundary."""


@dataclass(frozen=True)
class OHLCVRequest:
    symbol: str
    timeframe: str
    min_bars: int = 210  # enough for EMA200 + warm-up


def _resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = _RESAMPLE_RULE.get(timeframe)
    if rule is None:
        return df
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    resampled = df.resample(rule).agg(agg)
    return resampled.dropna(subset=["Open", "High", "Low", "Close"])


def _validate(df: pd.DataFrame, request: OHLCVRequest) -> pd.DataFrame:
    if df.empty:
        raise DataFetchError(f"empty OHLCV frame for {request.symbol}/{request.timeframe}")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataFetchError(f"missing columns {missing} for {request.symbol}")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if len(df) < request.min_bars:
        raise DataFetchError(
            f"insufficient bars for {request.symbol}/{request.timeframe}: "
            f"got {len(df)}, need {request.min_bars}"
        )
    return df


def fetch_ohlcv_yfinance(
    request: OHLCVRequest, max_retries: int = 3, backoff_seconds: float = 2.0
) -> pd.DataFrame | None:
    """Fetch OHLCV bars for `symbol` at `timeframe`. Returns None on failure."""
    import yfinance as yf  # imported lazily so tests can run without the dependency installed

    base_interval = _BASE_INTERVAL.get(request.timeframe)
    period = _MAX_PERIOD.get(request.timeframe)
    if base_interval is None or period is None:
        logger.error("unsupported timeframe %r", request.timeframe)
        return None

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            df = yf.download(
                tickers=request.symbol,
                period=period,
                interval=base_interval,
                progress=False,
                auto_adjust=False,
                multi_level_index=False,
            )
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = _resample(df, request.timeframe)
            df = _validate(df, request)
            return df
        except DataFetchError:
            raise
        except Exception as exc:  # network errors, rate limiting, yfinance internal errors
            last_error = exc
            wait = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "yfinance fetch failed for %s (%s), attempt %d/%d, retrying in %.1fs: %s",
                request.symbol, request.timeframe, attempt, max_retries, wait, exc,
            )
            if attempt < max_retries:
                time.sleep(wait)

    logger.error(
        "yfinance fetch permanently failed for %s/%s after %d attempts: %s",
        request.symbol, request.timeframe, max_retries, last_error,
    )
    return None


def fetch_ohlcv(request: OHLCVRequest) -> pd.DataFrame | None:
    """Public entry point Scanner 1 should call. Wraps provider selection +
    error containment so a bad symbol never raises out of a scan loop."""
    try:
        return fetch_ohlcv_yfinance(request)
    except DataFetchError as exc:
        logger.warning("data validation failed for %s/%s: %s", request.symbol, request.timeframe, exc)
        return None


def fetch_tv_confirmation(symbol: str, timeframe: str) -> dict | None:
    """Optional cross-check using TradingView's own server-side indicators.

    Returns a flat dict of indicator name -> value (e.g. "ADX", "RSI",
    "EMA20") or None if the symbol/exchange pairing can't be resolved or the
    request fails. Never used as the sole source of truth — yfinance-derived
    OHLCV remains authoritative for regime/strategy math.
    """
    try:
        from tradingview_ta import TA_Handler, Interval

        interval_map = {
            "15m": Interval.INTERVAL_15_MINUTES,
            "30m": Interval.INTERVAL_30_MINUTES,
            "1h": Interval.INTERVAL_1_HOUR,
            "4h": Interval.INTERVAL_4_HOURS,
            "1d": Interval.INTERVAL_1_DAY,
        }
        tv_interval = interval_map.get(timeframe)
        if tv_interval is None:
            logger.warning("no tradingview_ta interval mapping for %r", timeframe)
            return None

        handler = TA_Handler(
            symbol=symbol.replace("=X", "").upper(),
            exchange="FX_IDC",
            screener="forex",
            interval=tv_interval,
        )
        analysis = handler.get_analysis()
        return dict(analysis.indicators)
    except Exception as exc:
        logger.info("tradingview_ta confirmation unavailable for %s/%s: %s", symbol, timeframe, exc)
        return None
