"""Market-data acquisition for Twelve Data and explicitly selected Yahoo fallback.

Twelve Data supplies completed OHLCV candles and a timestamped midpoint price.
Its documented endpoints do not provide bid/ask, so midpoint quotes stay
explicitly midpoint-only and are never represented as executable top-of-book.
Yahoo remains historical/delayed fallback only and fails the publication gate.

Failures for candles return ``None`` so one unavailable instrument does not
stop a watchlist scan. Current-price failures raise ``DataFetchError`` and all
publication boundaries fail closed; no provider fallback is performed.
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

_TWELVEDATA_BASE_URL = "https://api.twelvedata.com"
_TWELVEDATA_INTERVALS = {
    "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day",
}
_TIMEFRAME_DELTAS = {
    "15m": timedelta(minutes=15), "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1),
}
_MAX_BAR_AGE = {
    "15m": timedelta(minutes=25), "30m": timedelta(minutes=45),
    "1h": timedelta(hours=1, minutes=15), "4h": timedelta(hours=4, minutes=15),
    "1d": timedelta(hours=30),
}
_SUPPORTED_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "CNH",
    "HKD", "SGD", "MXN", "ZAR", "TRY", "NOK", "SEK", "DKK", "PLN", "CZK",
    "HUF", "ILS", "THB", "INR", "KRW", "PHP", "IDR", "MYR", "TWD", "BRL",
    "RUB", "AED", "SAR", "QAR", "KWD", "BHD", "OMR", "CLP", "COP", "PEN",
    "ARS", "RON", "ISK",
})

# Basic has historically documented limits of 8 credits/minute and 800/day.
# Use headroom for dashboard/other consumers; this is process-local and cannot
# coordinate multiple app replicas, so Railway should run one scanner instance.
_LOCAL_CREDITS_PER_MINUTE = 7
_LOCAL_DAILY_CREDIT_CAP = 700
_LOCAL_CREDIT_SPACING_SECONDS = 60.0 / _LOCAL_CREDITS_PER_MINUTE


class DataFetchError(Exception):
    """Raised internally, always caught at the public-function boundary."""


@dataclass(frozen=True)
class OHLCVRequest:
    symbol: str
    timeframe: str
    min_bars: int = 210  # enough for EMA200 + warm-up


@dataclass(frozen=True)
class LiveQuote:
    """Read-only snapshot; midpoint feeds have no bid/ask fields by design."""

    instrument: str
    bid: float | None
    ask: float | None
    observed_at: datetime
    status: str
    price_basis: str = "bid_ask"
    midpoint: float | None = None

    @property
    def mid(self) -> float:
        if self.midpoint is not None:
            return self.midpoint
        if self.bid is None or self.ask is None:
            raise DataFetchError("quote has neither a midpoint nor bid/ask values")
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


class _TwelveDataBudget:
    """Conservative single-process credit pacing for the free plan."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._utc_day = datetime.now(timezone.utc).date()
        self._daily_credits = 0

    def reserve(self) -> None:
        with self._lock:
            today = datetime.now(timezone.utc).date()
            if today != self._utc_day:
                self._utc_day = today
                self._daily_credits = 0
            if self._daily_credits >= _LOCAL_DAILY_CREDIT_CAP:
                raise DataFetchError(
                    "Twelve Data local daily safety budget reached; requests paused until the next UTC day"
                )
            wait_for = _LOCAL_CREDIT_SPACING_SECONDS - (time.monotonic() - self._last_request)
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_request = time.monotonic()
            self._daily_credits += 1


_TWELVEDATA_BUDGET = _TwelveDataBudget()
_TWELVEDATA_QUOTE_CACHE: dict[tuple[str, str], tuple[float, LiveQuote]] = {}
_TWELVEDATA_QUOTE_CACHE_LOCK = threading.Lock()
_TWELVEDATA_QUOTE_CACHE_SECONDS = 5.0
_OHLCV_CACHE: dict[tuple[str, str, int], tuple[float, pd.DataFrame]] = {}
_OHLCV_CACHE_LOCK = threading.Lock()
_OHLCV_CACHE_SECONDS = 45.0

# yfinance has no native "4h" bar; pull hourly bars and resample.
_BASE_INTERVAL = {"15m": "15m", "30m": "30m", "1h": "1h", "4h": "1h", "1d": "1d"}
_MAX_PERIOD = {"15m": "60d", "30m": "60d", "1h": "730d", "4h": "730d", "1d": "5y"}
_RESAMPLE_RULE = {"4h": "4h"}
REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def normalize_twelvedata_symbol(symbol: str) -> str:
    """Map FX/metals aliases (EUR_USD, EURUSD=X, XAUUSD) to provider format."""
    raw = str(symbol).upper().strip()
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1]
    aliases = {"GC=F": "XAUUSD", "GC": "XAUUSD", "SI=F": "XAGUSD", "SI": "XAGUSD"}
    raw = aliases.get(raw, raw)
    normalized = raw.replace("=X", "").replace("/", "").replace("_", "").replace("-", "")
    if not re.fullmatch(r"[A-Z]{6}", normalized):
        raise DataFetchError(f"unsupported Twelve Data instrument {symbol!r}")
    base, quote = normalized[:3], normalized[3:]
    if normalized not in {"XAUUSD", "XAGUSD"} and (
        base == quote or base not in _SUPPORTED_CURRENCIES or quote not in _SUPPORTED_CURRENCIES
    ):
        raise DataFetchError(f"unsupported Twelve Data currency pair {normalized}")
    return f"{base}/{quote}"


def _twelvedata_api_key(settings=None) -> str:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    api_key = str(getattr(settings, "twelvedata_api_key", "") or "").strip()
    if not api_key:
        raise DataFetchError("TWELVEDATA_API_KEY is not configured")
    return api_key


def _twelvedata_get(path: str, params: dict[str, object], settings=None) -> dict:
    api_key = _twelvedata_api_key(settings)
    headers = {"Authorization": f"apikey {api_key}"}
    url = f"{_TWELVEDATA_BASE_URL}/{path.lstrip('/')}"
    last_error: Exception | None = None
    for attempt in range(2):
        _TWELVEDATA_BUDGET.reserve()
        try:
            response = httpx.get(url, headers=headers, params=params, timeout=8.0)
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
                continue
            raise DataFetchError(
                f"Twelve Data request failed ({type(exc).__name__}); this symbol will be skipped"
            ) from exc
        if response.status_code >= 500 and attempt == 0:
            time.sleep(1.0)
            continue
        if response.status_code == 401:
            raise DataFetchError("Twelve Data authentication failed (HTTP 401); check TWELVEDATA_API_KEY")
        if response.status_code == 403:
            raise DataFetchError("Twelve Data denied access (HTTP 403); verify plan coverage/permissions")
        if response.status_code == 429:
            raise DataFetchError("Twelve Data request limit reached (HTTP 429); this request was skipped")
        if response.status_code >= 500:
            raise DataFetchError(f"Twelve Data temporarily unavailable (HTTP {response.status_code})")
        if response.status_code != 200:
            raise DataFetchError(f"Twelve Data request failed (HTTP {response.status_code})")
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise DataFetchError("Twelve Data returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DataFetchError("Twelve Data returned an unexpected response shape")
        if str(payload.get("status", "")).lower() == "error" or "code" in payload and "message" in payload:
            code = str(payload.get("code", "unknown"))
            message = str(payload.get("message", "provider error"))[:180]
            if code == "429":
                raise DataFetchError("Twelve Data request credit/rate limit reached; request skipped")
            if code == "401":
                raise DataFetchError("Twelve Data rejected TWELVEDATA_API_KEY (provider code 401)")
            raise DataFetchError(f"Twelve Data API error {code}: {message}")
        return payload
    raise DataFetchError("Twelve Data request failed") from last_error


def fetch_ohlcv_twelvedata(
    request: OHLCVRequest, settings=None, *, now: datetime | None = None,
) -> pd.DataFrame:
    """Fetch complete UTC OHLCV bars and validate the existing scanner contract."""
    symbol = normalize_twelvedata_symbol(request.symbol)
    interval = _TWELVEDATA_INTERVALS.get(request.timeframe)
    duration = _TIMEFRAME_DELTAS.get(request.timeframe)
    if interval is None or duration is None:
        raise DataFetchError(f"unsupported Twelve Data timeframe {request.timeframe!r}")
    outputsize = min(5000, max(20, int(request.min_bars) + 10))
    payload = _twelvedata_get(
        "/time_series",
        {"symbol": symbol, "interval": interval, "outputsize": outputsize,
         "timezone": "UTC", "order": "ASC"},
        settings,
    )
    values = payload.get("values")
    if not isinstance(values, list):
        raise DataFetchError(f"Twelve Data returned no candle list for {symbol}/{interval}")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    rows: list[dict[str, float]] = []
    stamps: list[pd.Timestamp] = []
    volume_reported = True
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            stamp = pd.Timestamp(item["datetime"])
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            else:
                stamp = stamp.tz_convert("UTC")
            if (stamp + duration).to_pydatetime() > current:
                continue
            row = {
                "Open": float(item["open"]), "High": float(item["high"]),
                "Low": float(item["low"]), "Close": float(item["close"]),
            }
            raw_volume = item.get("volume")
            if raw_volume in (None, ""):
                volume_reported = False
                row["Volume"] = 0.0
            else:
                row["Volume"] = float(raw_volume)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DataFetchError("Twelve Data returned a malformed candle") from exc
        stamps.append(stamp)
        rows.append(row)
    if not rows:
        raise DataFetchError(f"Twelve Data returned no completed candles for {symbol}/{interval}")
    frame = pd.DataFrame(rows, index=pd.DatetimeIndex(stamps, name="Datetime"))
    frame = _validate(frame, request)
    frame.attrs["provider"] = "twelvedata"
    frame.attrs["price_basis"] = "aggregated_midpoint"
    frame.attrs["volume_reported"] = volume_reported
    if not volume_reported:
        logger.info("Twelve Data omitted volume for %s/%s; zero denotes unavailable, not measured volume", symbol, interval)
    _ensure_fresh(frame, request, now=current)
    return frame


def get_live_quote(symbol: str, settings=None, *, use_cache: bool = True) -> LiveQuote:
    """Return Twelve Data's fresh midpoint and timestamp; never synthesize bid/ask."""
    instrument = normalize_twelvedata_symbol(symbol)
    cache_key = (_TWELVEDATA_BASE_URL, instrument)
    if use_cache:
        with _TWELVEDATA_QUOTE_CACHE_LOCK:
            cached = _TWELVEDATA_QUOTE_CACHE.get(cache_key)
            if cached and time.monotonic() - cached[0] <= _TWELVEDATA_QUOTE_CACHE_SECONDS:
                return cached[1]
    payload = _twelvedata_get(
        "/quote", {"symbol": instrument, "timezone": "UTC"}, settings
    )
    try:
        price = float(payload["close"])
        timestamp = float(payload["last_quote_at"])
        observed_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        market_open = payload["is_market_open"]
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise DataFetchError("Twelve Data quote lacks a usable price, last_quote_at, or market-open status") from exc
    if not math.isfinite(price) or price <= 0 or not math.isfinite(timestamp) or timestamp <= 0:
        raise DataFetchError("Twelve Data returned an invalid midpoint or observation timestamp")
    status = "midpoint" if market_open is True else "closed"
    quote = LiveQuote(
        instrument=instrument, bid=None, ask=None, observed_at=observed_at,
        status=status, price_basis="midpoint", midpoint=price,
    )
    if use_cache:
        with _TWELVEDATA_QUOTE_CACHE_LOCK:
            _TWELVEDATA_QUOTE_CACHE[cache_key] = (time.monotonic(), quote)
    return quote


def ensure_live_quote_fresh(
    quote: LiveQuote, max_age_seconds: int = 90, now: datetime | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current.astimezone(timezone.utc) - quote.observed_at).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise DataFetchError("Twelve Data midpoint is in the future or outside the freshness limit")
    if quote.price_basis == "midpoint":
        if quote.status.lower() != "midpoint" or quote.bid is not None or quote.ask is not None:
            raise DataFetchError("Twelve Data price is not a valid open-market midpoint")
        if not math.isfinite(quote.mid) or quote.mid <= 0:
            raise DataFetchError("Twelve Data midpoint is invalid")
        return
    if quote.price_basis != "bid_ask" or quote.status.lower() != "tradeable":
        raise DataFetchError("quote lacks a valid live price basis/status")
    if quote.bid is None or quote.ask is None or not math.isfinite(quote.bid) or not math.isfinite(quote.ask):
        raise DataFetchError("quote has invalid bid/ask values")
    if quote.bid <= 0 or quote.ask < quote.bid:
        raise DataFetchError("quote has invalid bid/ask values")


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


def _ensure_fresh(df: pd.DataFrame, request: OHLCVRequest, now: datetime | None = None) -> None:
    """Reject stale/future completed bars before they can drive recommendations."""
    if request.timeframe not in _MAX_BAR_AGE:
        raise DataFetchError(f"no freshness policy for timeframe {request.timeframe!r}")
    latest = pd.Timestamp(df.index[-1])
    if latest.tzinfo is None:
        latest = latest.tz_localize("UTC")
    else:
        latest = latest.tz_convert("UTC")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = current.astimezone(timezone.utc) - latest.to_pydatetime()
    if age < timedelta(minutes=-5):
        raise DataFetchError(f"latest bar is in the future for {request.symbol}/{request.timeframe}")
    if age > _MAX_BAR_AGE[request.timeframe]:
        raise DataFetchError(f"latest bar is stale for {request.symbol}/{request.timeframe}")


def fetch_ohlcv_yfinance(
    request: OHLCVRequest, max_retries: int = 3, backoff_seconds: float = 2.0,
) -> pd.DataFrame | None:
    """Fetch OHLCV bars for ``symbol`` at ``timeframe`` from Yahoo."""
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
                tickers=request.symbol, period=period, interval=base_interval,
                progress=False, auto_adjust=False, multi_level_index=False,
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
                request.symbol, type(exc).__name__, attempt, max_retries, wait, exc,
            )
            if attempt < max_retries:
                time.sleep(wait)
    logger.error("yfinance fetch permanently failed for %s/%s after %d attempts: %s",
                 request.symbol, request.timeframe, max_retries, last_error)
    return None


def fetch_ohlcv(request: OHLCVRequest) -> pd.DataFrame | None:
    """Public entry point; provider failures skip only the affected scan request."""
    try:
        from config import get_settings

        settings = get_settings()
        provider = str(settings.data_provider).strip().lower()
        if provider == "twelvedata":
            cache_key = (normalize_twelvedata_symbol(request.symbol), request.timeframe, request.min_bars)
            with _OHLCV_CACHE_LOCK:
                cached = _OHLCV_CACHE.get(cache_key)
                if cached and time.monotonic() - cached[0] <= _OHLCV_CACHE_SECONDS:
                    frame = cached[1].copy()
                    frame.attrs.update(cached[1].attrs)
                    return frame
            frame = fetch_ohlcv_twelvedata(request, settings)
            frame.attrs["provider"] = "twelvedata"
            with _OHLCV_CACHE_LOCK:
                _OHLCV_CACHE[cache_key] = (time.monotonic(), frame.copy())
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
        logger.warning("OHLCV fetch unavailable for %s/%s (%s)",
                       request.symbol, request.timeframe, type(exc).__name__)
        return None


def fetch_tv_confirmation(symbol: str, timeframe: str) -> dict | None:
    """Optional TradingView indicator cross-check, never a price-data source."""
    try:
        from tradingview_ta import TA_Handler, Interval

        interval_map = {
            "15m": Interval.INTERVAL_15_MINUTES, "30m": Interval.INTERVAL_30_MINUTES,
            "1h": Interval.INTERVAL_1_HOUR, "4h": Interval.INTERVAL_4_HOURS,
            "1d": Interval.INTERVAL_1_DAY,
        }
        tv_interval = interval_map.get(timeframe)
        if tv_interval is None:
            logger.warning("no tradingview_ta interval mapping for %r", timeframe)
            return None
        handler = TA_Handler(
            symbol=symbol.replace("=X", "").upper(), exchange="FX_IDC",
            screener="forex", interval=tv_interval,
        )
        analysis = handler.get_analysis()
        return dict(analysis.indicators)
    except Exception as exc:
        logger.info("tradingview_ta confirmation unavailable for %s/%s: %s",
                    symbol, timeframe, exc)
        return None
