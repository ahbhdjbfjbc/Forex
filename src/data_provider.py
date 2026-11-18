"""OHLCV data acquisition.

`oanda` is the configured live-source adapter: it fetches completed v20
midpoint candles and separate timestamped account Bid/Ask quotes. `yfinance`
remains available only when explicitly selected, and its historical/delayed
bars are rejected by the live publication gate. `tradingview_ta` is optional
confirmation only and is not a price-data source for publication.

OHLCV fetches fail soft and return `None` when a symbol cannot be loaded, so
one bad symbol never kills a full watchlist scan. Current-quote calls raise a
typed `DataFetchError`; publication and webhook boundaries catch it and fail
closed rather than silently falling back to another price source.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
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
_OANDA_REST_URLS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}
_OANDA_GRANULARITIES = {"15m": "M15", "30m": "M30", "1h": "H1", "4h": "H4", "1d": "D"}
_OANDA_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "CNH",
    "HKD", "SGD", "MXN", "ZAR", "TRY", "NOK", "SEK", "DKK", "PLN", "CZK",
    "HUF", "ILS", "THB", "INR", "KRW", "PHP", "IDR", "MYR", "TWD", "BRL",
    "RUB", "AED", "SAR", "QAR", "KWD", "BHD", "OMR", "CLP", "COP", "PEN",
    "ARS", "RON", "ISK",
})
_OANDA_QUOTE_CACHE: dict[tuple[str, str, str], tuple[float, "LiveQuote"]] = {}
_OANDA_QUOTE_CACHE_LOCK = threading.Lock()
_OANDA_QUOTE_CACHE_SECONDS = 1.0

REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


class DataFetchError(Exception):
    """Raised internally, always caught at the public-function boundary."""


@dataclass(frozen=True)
class OHLCVRequest:
    symbol: str
    timeframe: str
    min_bars: int = 210  # enough for EMA200 + warm-up


@dataclass(frozen=True)
class LiveQuote:
    """OANDA top-of-book snapshot; read-only and never an execution receipt."""

    instrument: str
    bid: float
    ask: float
    observed_at: datetime
    status: str

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


def _oanda_instrument(symbol: str) -> str:
    raw = str(symbol).upper().strip()
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1]
    if raw in {"GC=F", "GC"}:
        raw = "XAUUSD"
    elif raw in {"SI=F", "SI"}:
        raw = "XAGUSD"
    normalized = raw.replace("=X", "").replace("/", "").replace("_", "")
    if not re.fullmatch(r"[A-Z]{6}", normalized):
        raise DataFetchError(f"unsupported OANDA instrument {symbol!r}")
    base, quote = normalized[:3], normalized[3:]
    if normalized not in {"XAUUSD", "XAGUSD"} and (
        base == quote or base not in _OANDA_CURRENCIES or quote not in _OANDA_CURRENCIES
    ):
        raise DataFetchError(f"unsupported OANDA currency pair {normalized}")
    return f"{base}_{quote}"


def _oanda_credentials(settings=None) -> tuple[str, str, str]:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    token = str(getattr(settings, "oanda_api_token", "") or "").strip()
    account_id = str(getattr(settings, "oanda_account_id", "") or "").strip()
    environment = str(getattr(settings, "oanda_environment", "live") or "live").strip().lower()
    if not token:
        raise DataFetchError("OANDA_API_TOKEN is not configured")
    if environment not in _OANDA_REST_URLS:
        raise DataFetchError("OANDA_ENVIRONMENT must be live or practice")
    return token, account_id, _OANDA_REST_URLS[environment]


def fetch_ohlcv_oanda(request: OHLCVRequest, settings=None) -> pd.DataFrame:
    """Fetch completed midpoint candles from OANDA v20; never falls back to Yahoo."""
    token, _account_id, base_url = _oanda_credentials(settings)
    instrument = _oanda_instrument(request.symbol)
    granularity = _OANDA_GRANULARITIES.get(request.timeframe)
    if granularity is None:
        raise DataFetchError(f"unsupported OANDA timeframe {request.timeframe!r}")
    count = min(5000, max(250, int(request.min_bars) + 10))
    try:
        response = httpx.get(
            f"{base_url}/v3/instruments/{instrument}/candles",
            headers={"Authorization": f"Bearer {token}"},
            params={"price": "M", "granularity": granularity, "count": count, "smooth": "false"},
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        raise DataFetchError(f"OANDA candles request failed ({type(exc).__name__})") from exc
    if response.status_code == 401:
        raise DataFetchError(
            "OANDA candles authentication failed (HTTP 401): verify OANDA_API_TOKEN "
            "and that OANDA_ENVIRONMENT matches the account that issued it"
        )
    if response.status_code == 403:
        raise DataFetchError(
            "OANDA denied candle access (HTTP 403): verify v20 API eligibility and account permissions"
        )
    if response.status_code != 200:
        raise DataFetchError(f"OANDA candles request failed (HTTP {response.status_code})")
    try:
        payload = response.json()
        candles = payload.get("candles") if isinstance(payload, dict) else None
    except (ValueError, TypeError) as exc:
        raise DataFetchError("OANDA returned invalid candle JSON") from exc
    if not isinstance(candles, list):
        raise DataFetchError("OANDA candle response has no candle list")

    rows: list[dict[str, float]] = []
    stamps: list[pd.Timestamp] = []
    for candle in candles:
        if not isinstance(candle, dict) or candle.get("complete") is not True:
            continue
        mid = candle.get("mid")
        if not isinstance(mid, dict):
            continue
        try:
            stamp = pd.Timestamp(candle["time"])
            if stamp.tzinfo is None:
                raise ValueError("OANDA candle timestamp has no timezone")
            stamp = stamp.tz_convert("UTC")
            row = {
                "Open": float(mid["o"]), "High": float(mid["h"]),
                "Low": float(mid["l"]), "Close": float(mid["c"]),
                "Volume": float(candle.get("volume", 0)),
            }
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DataFetchError("OANDA returned a malformed completed candle") from exc
        stamps.append(stamp)
        rows.append(row)
    if not rows:
        raise DataFetchError(f"OANDA returned no completed candles for {instrument}/{request.timeframe}")
    frame = pd.DataFrame(rows, index=pd.DatetimeIndex(stamps, name="Datetime"))
    frame = _validate(frame, request)
    frame.attrs["provider"] = "oanda"
    _ensure_fresh(frame, request)
    return frame


def get_oanda_live_quote(symbol: str, settings=None, *, use_cache: bool = True) -> LiveQuote:
    """Return a fresh read-only OANDA bid/ask quote for a supported instrument."""
    token, account_id, base_url = _oanda_credentials(settings)
    if not account_id:
        raise DataFetchError("OANDA_ACCOUNT_ID is required for current pricing")
    instrument = _oanda_instrument(symbol)
    cache_key = (base_url, account_id, instrument)
    now_monotonic = time.monotonic()
    if use_cache:
        with _OANDA_QUOTE_CACHE_LOCK:
            cached = _OANDA_QUOTE_CACHE.get(cache_key)
            if cached and now_monotonic - cached[0] <= _OANDA_QUOTE_CACHE_SECONDS:
                return cached[1]
    try:
        response = httpx.get(
            f"{base_url}/v3/accounts/{account_id}/pricing",
            headers={"Authorization": f"Bearer {token}"},
            params={"instruments": instrument},
            timeout=2.5,
        )
    except httpx.HTTPError as exc:
        raise DataFetchError(f"OANDA quote request failed ({type(exc).__name__})") from exc
    if response.status_code == 401:
        raise DataFetchError(
            "OANDA quote authentication failed (HTTP 401): verify OANDA_API_TOKEN, "
            "OANDA_ACCOUNT_ID, and matching OANDA_ENVIRONMENT"
        )
    if response.status_code == 403:
        raise DataFetchError(
            "OANDA denied quote access (HTTP 403): verify this account is authorized for v20 pricing"
        )
    if response.status_code == 404:
        raise DataFetchError(
            "OANDA quote account or instrument was not found (HTTP 404): verify OANDA_ACCOUNT_ID "
            "and OANDA_ENVIRONMENT"
        )
    if response.status_code != 200:
        raise DataFetchError(f"OANDA quote request failed (HTTP {response.status_code})")
    try:
        body = response.json()
        items = body.get("prices") if isinstance(body, dict) else None
        item = next(
            price for price in items or []
            if isinstance(price, dict) and price.get("instrument") == instrument
        )
        bid = float(item["bids"][0]["price"])
        ask = float(item["asks"][0]["price"])
        observed_at = datetime.fromisoformat(str(item["time"]).replace("Z", "+00:00"))
        status = str(item.get("status", "unknown"))
    except (ValueError, TypeError, KeyError, IndexError, StopIteration) as exc:
        raise DataFetchError("OANDA returned a malformed or incomplete quote") from exc
    if observed_at.tzinfo is None:
        raise DataFetchError("OANDA quote timestamp has no timezone")
    observed_at = observed_at.astimezone(timezone.utc)
    if not all(math.isfinite(value) and value > 0 for value in (bid, ask)) or ask < bid:
        raise DataFetchError("OANDA returned invalid bid/ask values")
    quote = LiveQuote(instrument, bid, ask, observed_at, status)
    if use_cache:
        with _OANDA_QUOTE_CACHE_LOCK:
            _OANDA_QUOTE_CACHE[cache_key] = (time.monotonic(), quote)
    return quote


def ensure_live_quote_fresh(quote: LiveQuote, max_age_seconds: int = 30,
                            now: datetime | None = None) -> None:
    current = now or datetime.now(timezone.utc)
    age = (current - quote.observed_at).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise DataFetchError("OANDA quote is in the future or outside the freshness limit")
    if quote.status.lower() != "tradeable":
        raise DataFetchError("OANDA instrument is not tradeable at the quote timestamp")


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
    if not isinstance(df.index, pd.DatetimeIndex):
        raise DataFetchError(f"OHLCV index is not datetime for {request.symbol}/{request.timeframe}")
    df = df.copy()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    for column in REQUIRED_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        raise DataFetchError(f"OHLCV has no complete price bars for {request.symbol}/{request.timeframe}")
    prices = df[["Open", "High", "Low", "Close"]].to_numpy(dtype=float)
    volumes = df["Volume"].to_numpy(dtype=float)
    if not bool(np.isfinite(prices).all()) or not bool(np.isfinite(volumes).all()):
        raise DataFetchError(f"OHLCV contains non-finite price or volume data for {request.symbol}/{request.timeframe}")
    invalid_range = (
        (df["High"] < df[["Open", "Close", "Low"]].max(axis=1))
        | (df["Low"] > df[["Open", "Close", "High"]].min(axis=1))
        | (df[["Open", "High", "Low", "Close"]] <= 0).any(axis=1)
        | (df["Volume"] < 0)
    )
    if bool(invalid_range.any()):
        raise DataFetchError(f"OHLCV contains impossible bars for {request.symbol}/{request.timeframe}")
    if len(df) < request.min_bars:
        raise DataFetchError(
            f"insufficient bars for {request.symbol}/{request.timeframe}: "
            f"got {len(df)}, need {request.min_bars}"
        )
    return df


_MAX_BAR_AGE = {
    "15m": timedelta(minutes=25),
    "30m": timedelta(minutes=45),
    "1h": timedelta(hours=1, minutes=15),
    "4h": timedelta(hours=4, minutes=15),
    "1d": timedelta(hours=30),
}


def _ensure_fresh(df: pd.DataFrame, request: OHLCVRequest, now: datetime | None = None) -> None:
    """Reject stale/future live bars before they can drive recommendations."""
    if request.timeframe not in _MAX_BAR_AGE:
        raise DataFetchError(f"no freshness policy for timeframe {request.timeframe!r}")
    latest = pd.Timestamp(df.index[-1])
    if latest.tzinfo is None:
        latest = latest.tz_localize("UTC")
    else:
        latest = latest.tz_convert("UTC")
    current = now or datetime.now(timezone.utc)
    age = current - latest.to_pydatetime()
    if age < timedelta(minutes=-5):
        raise DataFetchError(f"latest bar is in the future for {request.symbol}/{request.timeframe}")
    if age > _MAX_BAR_AGE[request.timeframe]:
        raise DataFetchError(f"latest bar is stale for {request.symbol}/{request.timeframe}")


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
            df.attrs["provider"] = "yfinance"
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
        from config import get_settings

        settings = get_settings()
        provider = str(settings.data_provider).strip().lower()
        if provider == "oanda":
            frame = fetch_ohlcv_oanda(request, settings)
        elif provider == "yfinance":
            frame = fetch_ohlcv_yfinance(request)
        else:
            logger.error("unsupported OHLCV provider %r; no recommendations will be produced", provider)
            return None
        if frame is None:
            return None
        _ensure_fresh(frame, request)
        return frame
    except DataFetchError as exc:
        logger.warning("data validation failed for %s/%s: %s", request.symbol, request.timeframe, exc)
        return None
    except Exception as exc:
        # Includes a missing optional SDK or unexpected provider failure; one
        # dependency problem must never escape into the watchlist scan loop.
        logger.warning("OHLCV fetch unavailable for %s/%s (%s)", request.symbol, request.timeframe, type(exc).__name__)
        return None


def fetch_tv_confirmation(symbol: str, timeframe: str) -> dict | None:
    """Optional cross-check using TradingView's own server-side indicators.

    Returns a flat dict of indicator name -> value (e.g. "ADX", "RSI",
    "EMA20") or None if the symbol/exchange pairing can't be resolved or the
    request fails. Never used as the sole source of truth; the selected OHLCV
    provider remains authoritative for regime/strategy math.
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
