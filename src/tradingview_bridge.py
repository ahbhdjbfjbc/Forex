"""TradingView Bridge — webhook receiver for Pine Script alerts.

Validates and parses incoming TradingView alert JSON (see `TradingViewAlert`
in `src/models.py`), checks the shared-secret to reject spoofed traffic, and
converts a valid alert into a `TradeSignal` for the notifier. This module
owns HTTP parsing/validation only — no indicator math, no regime/strategy
logic (that belongs to Scanner 1 / Scanner 2).

Run standalone with:
    uvicorn src.tradingview_bridge:app --host 0.0.0.0 --port 8000

Pine Script side: an alert's "Message" field should be a JSON string matching
`TradingViewAlert`, e.g.:
    {{"ticker": "{{ticker}}", "timeframe": "{{interval}}", "signal": "BUY",
      "price": {{close}}, "secret": "<WEBHOOK_SHARED_SECRET>"}}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import threading
import time
import asyncio
from dataclasses import replace
from collections import deque
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from config import get_settings
from src import risk
from src.models import (
    MarketRegime,
    RiskParameters,
    StrategyType,
    TradeDirection,
    TradeSignal,
    TradingViewAlert,
    WebhookAck,
)
from src.notifier import flush_pending_signal_outcomes, send_alert, send_signal_outcome
from src.scanner_1_regime import scan_regime_with_data
from src.scanner_2_strategy import generate_signal
from src.signal_lifecycle import signal_lifecycle
from src.symbol_resolver import instrument_type, sanitize_symbol, to_yfinance_ticker

logger = logging.getLogger(__name__)

app = FastAPI(title="Forex Scanner — TradingView Bridge")

# Small in-memory ring buffer for debugging/inspection; not a persistence layer.
_RECENT_ALERTS: deque[TradingViewAlert] = deque(maxlen=100)
# This short cache prevents immediate retries of byte-identical payloads. The
# durable active-signal registry separately suppresses the same live setup
# when price changes or a new bar produces a fresh TradingView alert.
_ALERT_DEDUPE_SECONDS = 15.0
_ALERT_DEDUPE_MAX = 2048
_SEEN_ALERTS: dict[str, float] = {}
_ALERT_DEDUPE_LOCK = threading.Lock()

# The webhook is designed for automated TradingView alerts, not arbitrary
# high-volume clients. Keep a bounded per-peer sliding window in this single
# process. Do not trust X-Forwarded-For directly: the proxy must be explicitly
# configured as trusted by the ASGI server for request.client.host to reflect
# the real peer safely.
_REQUEST_RATE_WINDOW_SECONDS = 60.0
_REQUEST_RATE_MAX = 120
_REQUEST_RATE_MAX_CLIENTS = 4096
_REQUESTS_BY_CLIENT: dict[str, deque[float]] = {}
_REQUEST_RATE_LOCK = threading.Lock()

# Published by TradingView for allowlisting webhook senders:
# https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/
# Enforcement is opt-in (WEBHOOK_ENFORCE_TV_IPS=true) because behind a hosting
# proxy request.client.host is only the real sender when the ASGI server trusts
# that proxy (uvicorn: FORWARDED_ALLOW_IPS).
TRADINGVIEW_WEBHOOK_IPS = frozenset({"52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"})
_LAST_ALERT_AT: datetime | None = None
_LAST_ALERT_LOCK = threading.Lock()


def _record_alert_received(now: datetime | None = None) -> None:
    global _LAST_ALERT_AT
    with _LAST_ALERT_LOCK:
        _LAST_ALERT_AT = now or datetime.now(timezone.utc)


def last_tradingview_alert_at() -> datetime | None:
    with _LAST_ALERT_LOCK:
        return _LAST_ALERT_AT


def last_tradingview_alert_label() -> str:
    stamp = last_tradingview_alert_at()
    if stamp is None:
        return "لم يصل أي تنبيه موثّق منذ آخر تشغيل"
    minutes = int((datetime.now(timezone.utc) - stamp).total_seconds() // 60)
    return f"{stamp.strftime('%Y-%m-%d %H:%M')} UTC (قبل {minutes} دقيقة)"


def tradingview_heartbeat_ok(settings=None, now: datetime | None = None) -> tuple[bool, str]:
    """Heartbeat for admins who expect regular TradingView alerts (0 = disabled)."""
    settings = settings or get_settings()
    minutes = int(getattr(settings, "tradingview_heartbeat_minutes", 0) or 0)
    if minutes <= 0:
        return True, "disabled"
    stamp = last_tradingview_alert_at()
    now = now or datetime.now(timezone.utc)
    if stamp is None or (now - stamp).total_seconds() > minutes * 60:
        return False, f"لم يصل تنبيه TradingView موثّق منذ أكثر من {minutes} دقيقة"
    return True, "ok"


_TIMEFRAME_DURATION = {
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}
_STRATEGY_ALIASES = {
    "trend_pullback": StrategyType.TREND_PULLBACK,
    "mean_reversion": StrategyType.MEAN_REVERSION_FADE,
    "mean_reversion_fade": StrategyType.MEAN_REVERSION_FADE,
    "volatility_breakout": StrategyType.VOLATILITY_BREAKOUT,
    "breakout": StrategyType.VOLATILITY_BREAKOUT,
    "pine_bridge": StrategyType.EXTERNAL_ALERT,
    "tradingview": StrategyType.EXTERNAL_ALERT,
    "external_alert": StrategyType.EXTERNAL_ALERT,
}


class MarketDataUnavailable(RuntimeError):
    """Fresh market data could not be fetched for corroboration."""


class AlertNotCorroborated(RuntimeError):
    """The internal engine did not confirm the external alert."""

# Prevent stocks, indices, futures codes, and malformed symbols from silently
# falling through instrument_type() to FX pip/lot sizing.
_SUPPORTED_FX_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "CNH",
    "HKD", "SGD", "MXN", "ZAR", "TRY", "NOK", "SEK", "DKK", "PLN", "CZK",
    "HUF", "ILS", "THB", "INR", "KRW", "PHP", "IDR", "MYR", "TWD", "BRL",
    "RUB", "AED", "SAR", "QAR", "KWD", "BHD", "OMR", "CLP", "COP", "PEN",
    "ARS", "RON", "ISK",
})


def _is_supported_fx_pair(ticker: str) -> bool:
    normalized = ticker.upper().replace("=X", "").replace("/", "").strip()
    return (
        len(normalized) == 6
        and normalized.isalpha()
        and normalized[:3] in _SUPPORTED_FX_CURRENCIES
        and normalized[3:] in _SUPPORTED_FX_CURRENCIES
        and normalized[:3] != normalized[3:]
    )


def _verify_secret(provided: str | None) -> None:
    expected = get_settings().webhook_shared_secret
    # Fail closed when the deployment still uses the public example/default
    # secret or a trivially short value. Never accept the placeholder.
    if (not expected or expected == "change-me-to-a-long-random-string"
            or expected.startswith("REPLACE_WITH_") or len(expected) < 32
            or not provided or not hmac.compare_digest(provided, expected)):
        raise HTTPException(status_code=403, detail="invalid or missing webhook secret")


def _alert_fingerprint(alert: TradingViewAlert) -> str:
    # Pydantic excludes the webhook secret from model dumps by design.
    public_payload = alert.model_dump(mode="json")
    serialized = json.dumps(public_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _claim_alert_fingerprint(fingerprint: str) -> bool:
    """Atomically claim an alert fingerprint; False means a recent replay."""
    now = time.monotonic()
    with _ALERT_DEDUPE_LOCK:
        expired = [key for key, seen_at in _SEEN_ALERTS.items()
                   if now - seen_at >= _ALERT_DEDUPE_SECONDS]
        for key in expired:
            _SEEN_ALERTS.pop(key, None)
        if fingerprint in _SEEN_ALERTS:
            return False
        if len(_SEEN_ALERTS) >= _ALERT_DEDUPE_MAX:
            oldest = min(_SEEN_ALERTS, key=_SEEN_ALERTS.get)
            _SEEN_ALERTS.pop(oldest, None)
        _SEEN_ALERTS[fingerprint] = now
        return True


def _release_alert_fingerprint(fingerprint: str) -> None:
    with _ALERT_DEDUPE_LOCK:
        _SEEN_ALERTS.pop(fingerprint, None)


def _validate_alert_time(alert: TradingViewAlert, now: datetime | None = None) -> None:
    """Require a trustworthy bar timestamp and reject future/stale alerts."""
    if not alert.bar_time:
        raise HTTPException(status_code=422, detail="bar_time is required for TradingView alerts")
    raw = alert.bar_time.strip()
    try:
        if raw.isdigit():
            epoch = float(raw)
            if len(raw) >= 13:
                epoch /= 1000.0
            event_time = datetime.fromtimestamp(epoch, tz=timezone.utc)
        else:
            event_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            event_time = event_time.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise HTTPException(status_code=422, detail="bar_time is invalid") from exc

    current = now or datetime.now(timezone.utc)
    age = current - event_time
    if age < timedelta(minutes=-2):
        raise HTTPException(status_code=422, detail="bar_time is in the future")
    max_age = _TIMEFRAME_DURATION[alert.timeframe] + timedelta(minutes=10)
    if age > max_age:
        raise HTTPException(status_code=422, detail="TradingView alert is stale")


def _strategy_for_alert(strategy_name: str | None) -> StrategyType:
    """Map only recognized labels; unknown external labels remain rejected."""
    if not strategy_name:
        return StrategyType.EXTERNAL_ALERT
    normalized = strategy_name.strip().lower().replace(" ", "_")
    try:
        return _STRATEGY_ALIASES[normalized]
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="unsupported TradingView strategy label") from exc


def _corroborate_alert(alert: TradingViewAlert):
    """Require a fresh internal scan to confirm direction and approximate price."""
    settings = get_settings()
    if str(getattr(settings, "data_provider", "") or "").strip().lower() != "oanda":
        raise MarketDataUnavailable("TradingView alerts require DATA_PROVIDER=oanda")
    raw_symbol = alert.ticker.upper().replace("=X", "").replace("=F", "")
    symbol = sanitize_symbol(raw_symbol)
    if symbol is None:
        raise HTTPException(status_code=422, detail="unsupported ticker for market corroboration")
    outcome = scan_regime_with_data(
        symbol,
        alert.timeframe,
        settings,
        fetch_symbol=to_yfinance_ticker(symbol),
    )
    if outcome is None:
        raise MarketDataUnavailable(f"no fresh OHLCV data for {symbol}/{alert.timeframe}")
    regime_result, frame = outcome
    if frame is None or frame.empty or "Close" not in frame.columns:
        raise MarketDataUnavailable(f"empty OHLCV data for {symbol}/{alert.timeframe}")

    try:
        from src.data_provider import ensure_live_quote_fresh, get_oanda_live_quote

        quote = get_oanda_live_quote(symbol, settings)
        ensure_live_quote_fresh(
            quote,
            int(getattr(settings, "oanda_quote_max_age_seconds", 30)),
        )
    except Exception as exc:
        raise MarketDataUnavailable(f"fresh OANDA quote unavailable for {symbol}") from exc

    market_price = quote.mid
    atr = float(regime_result.metrics.atr)
    tolerance = max(quote.spread * 3.0, abs(market_price) * 0.0001, atr * 0.20)
    if not math.isfinite(market_price) or abs(alert.price - market_price) > tolerance:
        raise AlertNotCorroborated("TradingView price does not match the fresh OANDA bid/ask market")

    if alert.signal == "NONE":
        return None, regime_result, frame

    signal = generate_signal(regime_result, frame, settings)
    if signal is None or signal.direction.value != alert.signal:
        raise AlertNotCorroborated("internal scanner did not produce a same-direction qualified signal")
    return signal, regime_result, frame


def _apply_tradingview_levels(signal: TradeSignal, alert: TradingViewAlert, settings) -> TradeSignal:
    """Use validated Pine levels after shifting them to the OANDA side quote."""
    from src.data_provider import ensure_live_quote_fresh, get_oanda_live_quote

    quote = get_oanda_live_quote(alert.ticker, settings)
    ensure_live_quote_fresh(
        quote,
        int(getattr(settings, "oanda_quote_max_age_seconds", 30)),
    )
    entry = quote.ask if signal.direction == TradeDirection.BUY else quote.bid
    shift = entry - alert.price
    adjusted_alert = alert.model_copy(update={
        "price": entry,
        "stop_loss": float(alert.stop_loss) + shift,
        "take_profit": float(alert.take_profit) + shift,
        "take_profit_2": float(alert.take_profit_2) + shift,
    })
    levels = _alert_to_signal(adjusted_alert)
    return replace(
        signal,
        entry_price=levels.entry_price,
        stop_loss_price=levels.stop_loss_price,
        stop_loss_pips=levels.stop_loss_pips,
        take_profit_1_price=levels.take_profit_1_price,
        take_profit_1_pips=levels.take_profit_1_pips,
        take_profit_1_rr=levels.take_profit_1_rr,
        take_profit_2_price=levels.take_profit_2_price,
        take_profit_2_pips=levels.take_profit_2_pips,
        take_profit_2_rr=levels.take_profit_2_rr,
        risk=levels.risk,
        rationale=f"{signal.rationale}; tested Pine levels rebased to OANDA {quote.instrument} quote",
    )


def _enforce_request_rate_limit(client_host: str) -> None:
    """Limit webhook requests per actual ASGI peer address (process-local)."""
    now = time.monotonic()
    key = client_host or "unknown"
    with _REQUEST_RATE_LOCK:
        history = _REQUESTS_BY_CLIENT.get(key)
        if history is None:
            if len(_REQUESTS_BY_CLIENT) >= _REQUEST_RATE_MAX_CLIENTS:
                # Discard expired peers first, then the least recently active
                # peer to keep memory bounded during source-IP churn.
                for known_key, events in list(_REQUESTS_BY_CLIENT.items()):
                    while events and now - events[0] >= _REQUEST_RATE_WINDOW_SECONDS:
                        events.popleft()
                    if not events:
                        _REQUESTS_BY_CLIENT.pop(known_key, None)
                if len(_REQUESTS_BY_CLIENT) >= _REQUEST_RATE_MAX_CLIENTS:
                    oldest_key = min(
                        _REQUESTS_BY_CLIENT,
                        key=lambda known_key: _REQUESTS_BY_CLIENT[known_key][-1],
                    )
                    _REQUESTS_BY_CLIENT.pop(oldest_key, None)
            history = deque()
            _REQUESTS_BY_CLIENT[key] = history

        while history and now - history[0] >= _REQUEST_RATE_WINDOW_SECONDS:
            history.popleft()
        if len(history) >= _REQUEST_RATE_MAX:
            retry_after = max(1, int(_REQUEST_RATE_WINDOW_SECONDS - (now - history[0])))
            raise HTTPException(
                status_code=429,
                detail="too many webhook requests; retry later",
                headers={"Retry-After": str(retry_after)},
            )
        history.append(now)


def _alert_to_signal(alert: TradingViewAlert) -> TradeSignal:
    """Convert a validated TradingView alert into a risk-checked signal.

    FX pairs use pip/standard-lot math. Metals and crypto use price distance
    and raw instrument units; their contract/lot conventions vary by broker.
    """
    settings = get_settings()
    direction = TradeDirection(alert.signal)
    kind = instrument_type(alert.ticker)
    strategy = _strategy_for_alert(alert.strategy)
    if kind == "FX" and not _is_supported_fx_pair(alert.ticker):
        raise HTTPException(status_code=422, detail="unsupported ticker for FX risk sizing")

    if not math.isfinite(alert.price) or alert.price <= 0:
        raise HTTPException(status_code=422, detail="price must be a finite positive number")

    if alert.stop_loss is None or alert.take_profit is None or alert.take_profit_2 is None:
        raise HTTPException(
            status_code=422,
            detail="stop_loss, take_profit and take_profit_2 are required; no default levels are applied",
        )
    stop_loss_price = float(alert.stop_loss)

    if not math.isfinite(stop_loss_price) or stop_loss_price <= 0:
        raise HTTPException(status_code=422, detail="stop_loss must be a finite positive number")
    # A stop on the wrong side would produce misleading targets and position size.
    if direction == TradeDirection.BUY and stop_loss_price >= alert.price:
        raise HTTPException(status_code=422, detail="BUY stop_loss must be below entry price")
    if direction == TradeDirection.SELL and stop_loss_price <= alert.price:
        raise HTTPException(status_code=422, detail="SELL stop_loss must be above entry price")

    stop_distance = abs(alert.price - stop_loss_price)
    tp1_price = float(alert.take_profit)
    tp2_price = float(alert.take_profit_2)
    if direction == TradeDirection.BUY and not (alert.price < tp1_price < tp2_price):
        raise HTTPException(status_code=422, detail="BUY targets must be above entry and TP2 above TP1")
    if direction == TradeDirection.SELL and not (tp2_price < tp1_price < alert.price):
        raise HTTPException(status_code=422, detail="SELL targets must be below entry and TP2 below TP1")
    tp1_rr = abs(tp1_price - alert.price) / stop_distance
    tp2_rr = abs(tp2_price - alert.price) / stop_distance
    if tp1_rr < settings.min_risk_reward:
        raise HTTPException(status_code=422, detail="TP1 does not meet configured minimum risk/reward")
    if tp2_rr <= tp1_rr:
        raise HTTPException(status_code=422, detail="TP2 must have a greater risk/reward than TP1")

    if (not math.isfinite(tp1_price) or not math.isfinite(tp2_price)
            or tp1_price <= 0 or tp2_price <= 0):
        raise HTTPException(status_code=422, detail="computed take-profit prices must be finite and positive")
    if direction == TradeDirection.BUY and not (alert.price < tp1_price < tp2_price):
        raise HTTPException(status_code=422, detail="BUY take-profit targets must be above entry and ordered")
    if direction == TradeDirection.SELL and not (tp2_price < tp1_price < alert.price):
        raise HTTPException(status_code=422, detail="SELL take-profit targets must be below entry and ordered")

    if kind == "FX":
        risk_params: RiskParameters = risk.build_risk_parameters(
            alert.ticker, settings.account_size_usd, settings.risk_percent_per_trade,
            alert.price, stop_loss_price
        )
        sl_distance = risk_params.stop_loss_pips
        tp1_distance = round(risk.price_to_pips(alert.ticker, tp1_price - alert.price), 1)
        tp2_distance = round(risk.price_to_pips(alert.ticker, tp2_price - alert.price), 1)
    else:
        # The existing TradeSignal model reuses these fields for non-FX assets:
        # distances are price units and lot_size carries raw units (BTC/ETH/oz).
        unit_size = risk.calculate_unit_size(
            settings.account_size_usd, settings.risk_percent_per_trade, alert.price, stop_loss_price
        )
        risk_params = RiskParameters(
            account_size_usd=settings.account_size_usd,
            risk_percent=settings.risk_percent_per_trade,
            stop_loss_pips=round(abs(alert.price - stop_loss_price), 4),
            pip_value_per_lot=0.0,
            lot_size=unit_size,
            risk_amount_usd=round(settings.account_size_usd * settings.risk_percent_per_trade / 100, 2),
        )
        sl_distance = risk_params.stop_loss_pips
        tp1_distance = round(abs(tp1_price - alert.price), 4)
        tp2_distance = round(abs(tp2_price - alert.price), 4)

    return TradeSignal(
        symbol=alert.ticker,
        timeframe=alert.timeframe,
        direction=direction,
        strategy=strategy,
        regime=MarketRegime.STRONG_TREND_BULLISH if direction == TradeDirection.BUY else MarketRegime.STRONG_TREND_BEARISH,
        entry_price=alert.price,
        stop_loss_price=round(stop_loss_price, 8),
        stop_loss_pips=sl_distance,
        take_profit_1_price=round(tp1_price, 8),
        take_profit_1_pips=tp1_distance,
        take_profit_1_rr=tp1_rr,
        take_profit_2_price=round(tp2_price, 8),
        take_profit_2_pips=tp2_distance,
        take_profit_2_rr=tp2_rr,
        risk=risk_params,
        rationale=f"TradingView Pine alert ({alert.strategy or 'unspecified strategy'})",
        timestamp=datetime.now(timezone.utc),
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/health/market")
def market_health(
    symbol: str = "EURUSD",
    x_webhook_secret: str | None = Header(default=None),
):
    """Secret-protected live OANDA diagnostic; never submits an order."""
    _verify_secret(x_webhook_secret)
    settings = get_settings()
    if str(getattr(settings, "data_provider", "") or "").strip().lower() != "oanda":
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "reason": "DATA_PROVIDER is not oanda"},
        )
    try:
        from src.data_provider import (
            DataFetchError,
            ensure_live_quote_fresh,
            get_oanda_live_quote,
        )

        quote = get_oanda_live_quote(symbol, settings, use_cache=False)
        ensure_live_quote_fresh(
            quote,
            int(getattr(settings, "oanda_quote_max_age_seconds", 30)),
        )
    except (DataFetchError, TypeError, ValueError) as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "reason": str(exc)[:200],
                "provider": "oanda",
            },
        )
    age_seconds = max(
        0.0,
        (datetime.now(timezone.utc) - quote.observed_at).total_seconds(),
    )
    return {
        "status": "ok",
        "provider": "oanda",
        "instrument": quote.instrument,
        "bid": quote.bid,
        "ask": quote.ask,
        "observed_at": quote.observed_at.isoformat(),
        "age_seconds": round(age_seconds, 3),
        "trade_status": quote.status,
    }


def _recent_alerts_payload() -> list[dict]:
    """Build an inspection payload with credential fields excluded."""
    return [alert.model_dump() for alert in _RECENT_ALERTS]


@app.get("/webhook/recent")
def recent_alerts(x_webhook_secret: str | None = Header(default=None)) -> list[dict]:
    """Return recent alerts only to callers that prove possession of the secret."""
    _verify_secret(x_webhook_secret)
    return _recent_alerts_payload()


MAX_WEBHOOK_BODY_BYTES = 16 * 1024


@app.post("/webhook/tradingview", response_model=WebhookAck)
async def tradingview_webhook(request: Request) -> WebhookAck:
    # Reject abusive request rates before parsing the body or doing risk math.
    peer = request.client.host if request.client is not None else "unknown"
    if getattr(get_settings(), "webhook_enforce_tv_ips", False) and peer not in TRADINGVIEW_WEBHOOK_IPS:
        logger.warning("rejected webhook from a non-TradingView address")
        raise HTTPException(status_code=403, detail="sender not allowed")
    _enforce_request_rate_limit(peer)

    # Bound input size before parsing; TradingView alerts are tiny JSON objects.
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content length")

    chunks: list[bytes] = []
    body_size = 0
    try:
        async for chunk in request.stream():
            body_size += len(chunk)
            if body_size > MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="request body too large")
            chunks.append(chunk)
        body = json.loads(b"".join(chunks).decode("utf-8"))
    except HTTPException:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("rejected malformed TradingView request body (%s)", type(exc).__name__)
        raise HTTPException(status_code=400, detail="malformed JSON body") from exc
    except Exception as exc:
        logger.warning("failed reading TradingView request body (%s)", type(exc).__name__)
        raise HTTPException(status_code=400, detail="malformed JSON body") from exc

    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="invalid alert payload")

    try:
        alert = TradingViewAlert.model_validate(body)
    except Exception as exc:
        # Pydantic's exception string can include input values. Never put it in
        # logs or HTTP responses because the input contains a shared secret.
        logger.warning("rejected malformed TradingView alert (%s)", type(exc).__name__)
        raise HTTPException(status_code=422, detail="invalid alert payload") from exc

    _verify_secret(alert.secret)
    _record_alert_received()
    _validate_alert_time(alert)
    _strategy_for_alert(alert.strategy)
    if alert.signal != "NONE":
        try:
            _alert_to_signal(alert)  # validates provided stop/target geometry; never published
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("rejected TradingView alert due to invalid risk inputs (%s)", type(exc).__name__)
            raise HTTPException(status_code=422, detail="invalid risk inputs in TradingView alert") from exc
    logger.info("received TradingView alert: %s %s @ %.5f (%s)", alert.ticker, alert.signal, alert.price, alert.timeframe)

    fingerprint = _alert_fingerprint(alert)
    if not _claim_alert_fingerprint(fingerprint):
        return WebhookAck(status="duplicate_suppressed", detail="identical alert was already received")

    flush_pending_signal_outcomes()
    try:
        # The scanner must independently confirm the signal direction and
        # current price from fresh OANDA-backed OHLCV/quote data. Explicit Pine
        # stop/target levels are applied only after that corroboration and are
        # rebased to the current side-specific OANDA quote below.
        signal, _regime_result, frame = await asyncio.to_thread(_corroborate_alert, alert)
    except AlertNotCorroborated:
        _RECENT_ALERTS.append(alert.model_copy(update={"secret": None}))
        logger.info("TradingView alert not corroborated for %s/%s; no recommendation published", alert.ticker, alert.timeframe)
        return WebhookAck(
            status="not_corroborated",
            detail="fresh project data or the internal strategy did not confirm the alert; nothing was published",
        )
    except MarketDataUnavailable:
        _release_alert_fingerprint(fingerprint)
        logger.warning("market data unavailable for TradingView alert %s/%s", alert.ticker, alert.timeframe)
        return JSONResponse(
            status_code=503,
            content=WebhookAck(status="market_data_unavailable", detail="fresh market data unavailable; no recommendation was published").model_dump(),
        )
    except HTTPException:
        _release_alert_fingerprint(fingerprint)
        raise
    except Exception as exc:
        _release_alert_fingerprint(fingerprint)
        logger.error("could not corroborate TradingView alert for %s (%s)", alert.ticker, type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=WebhookAck(status="market_validation_failed", detail="internal market validation failed; no recommendation was published").model_dump(),
        )

    # Never retain the credential after verification.
    _RECENT_ALERTS.append(alert.model_copy(update={"secret": None}))
    if signal is None:
        try:
            outcome = signal_lifecycle.observe_frame(alert.ticker, alert.timeframe, frame)
            if outcome is not None:
                send_signal_outcome(outcome)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            _release_alert_fingerprint(fingerprint)
            logger.exception("could not evaluate TradingView reset for %s/%s", alert.ticker, alert.timeframe)
            return JSONResponse(
                status_code=503,
                content=WebhookAck(status="outcome_check_failed", detail="fresh price data could not be applied to the open recommendation").model_dump(),
            )
        signal_lifecycle.reset_block(alert.ticker, alert.timeframe)
        return WebhookAck(status="reset", detail="fresh market data confirmed no active TradingView setup")

    try:
        signal = await asyncio.to_thread(
            _apply_tradingview_levels, signal, alert, get_settings()
        )
    except Exception as exc:
        _release_alert_fingerprint(fingerprint)
        logger.warning("could not apply validated TradingView levels for %s (%s)", alert.ticker, type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=WebhookAck(
                status="live_quote_unavailable",
                detail="fresh OANDA quote or validated TradingView levels unavailable; no recommendation was published",
            ).model_dump(),
        )

    try:
        dispatch_results = await asyncio.to_thread(
            send_alert,
            signal,
            source="tradingview",
            entry_bar_id=str(frame.index[-1]),
            market_frame=frame,
        )
    except Exception as exc:
        _release_alert_fingerprint(fingerprint)
        logger.error("failed to dispatch alert for %s (%s)", alert.ticker, type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=WebhookAck(status="delivery_failed", detail="alert stored but notification dispatch failed").model_dump(),
        )

    blocked_reason = getattr(dispatch_results, "blocked_reason", None)
    if blocked_reason:
        _release_alert_fingerprint(fingerprint)
        return WebhookAck(status="blocked_by_filters", detail=str(blocked_reason)[:300])

    if isinstance(dispatch_results, dict) and dispatch_results.get("duplicate"):
        return WebhookAck(
            status="duplicate_suppressed",
            detail=f"{alert.ticker}/{alert.timeframe} already has an active or recently closed setup",
        )

    if not isinstance(dispatch_results, dict) or not any(
        value is True for channel, value in dispatch_results.items() if channel != "duplicate"
    ):
        # Permit a caller/TradingView retry after a transient no-channel failure.
        _release_alert_fingerprint(fingerprint)
        logger.warning("alert validated for %s but no notification channel reported delivery", alert.ticker)
        return JSONResponse(
            status_code=503,
            content=WebhookAck(
                status="delivery_failed",
                detail="alert validated and stored, but no notification channel reported delivery",
            ).model_dump(),
        )

    return WebhookAck(status="ok", detail=f"alert processed for {alert.ticker}; at least one channel reported delivery")
