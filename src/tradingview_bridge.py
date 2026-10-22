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
from collections import deque
from datetime import datetime, timezone

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
from src.notifier import send_alert
from src.symbol_resolver import instrument_type

logger = logging.getLogger(__name__)

app = FastAPI(title="Forex Scanner — TradingView Bridge")

# Small in-memory ring buffer for debugging/inspection; not a persistence layer.
_RECENT_ALERTS: deque[TradingViewAlert] = deque(maxlen=100)
# Short-window in-process deduplication prevents TradingView retries/replays of
# the exact same alert from spamming subscribers. This is not durable across
# process restarts; it is a defensive mitigation, not a replacement for TLS.
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

DEFAULT_FALLBACK_SL_PIPS = 20.0  # FX fallback only
# No standard pip convention exists for metals/crypto. Use conservative
# price-relative fallback distances if TradingView omits stop_loss. These are
# signal-generation defaults, not a claim about broker contract specifications.
METAL_FALLBACK_SL_PERCENT = 0.5
CRYPTO_FALLBACK_SL_PERCENT = 1.0

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
    if kind == "FX" and not _is_supported_fx_pair(alert.ticker):
        raise HTTPException(status_code=422, detail="unsupported ticker for FX risk sizing")

    if not math.isfinite(alert.price) or alert.price <= 0:
        raise HTTPException(status_code=422, detail="price must be a finite positive number")

    if alert.stop_loss is not None:
        stop_loss_price = float(alert.stop_loss)
    elif kind == "FX":
        offset = DEFAULT_FALLBACK_SL_PIPS * risk.pip_size(alert.ticker)
        stop_loss_price = alert.price - offset if direction == TradeDirection.BUY else alert.price + offset
    else:
        fallback_percent = METAL_FALLBACK_SL_PERCENT if kind == "METAL" else CRYPTO_FALLBACK_SL_PERCENT
        offset = alert.price * fallback_percent / 100.0
        stop_loss_price = alert.price - offset if direction == TradeDirection.BUY else alert.price + offset

    if not math.isfinite(stop_loss_price) or stop_loss_price <= 0:
        raise HTTPException(status_code=422, detail="stop_loss must be a finite positive number")
    # A stop on the wrong side would produce misleading targets and position size.
    if direction == TradeDirection.BUY and stop_loss_price >= alert.price:
        raise HTTPException(status_code=422, detail="BUY stop_loss must be below entry price")
    if direction == TradeDirection.SELL and stop_loss_price <= alert.price:
        raise HTTPException(status_code=422, detail="SELL stop_loss must be above entry price")

    stop_distance = abs(alert.price - stop_loss_price)
    if alert.take_profit is not None:
        tp1_price = float(alert.take_profit)
        if direction == TradeDirection.BUY and tp1_price <= alert.price:
            raise HTTPException(status_code=422, detail="BUY take_profit must be above entry price")
        if direction == TradeDirection.SELL and tp1_price >= alert.price:
            raise HTTPException(status_code=422, detail="SELL take_profit must be below entry price")
        tp1_rr = abs(tp1_price - alert.price) / stop_distance
        if tp1_rr < settings.min_risk_reward:
            raise HTTPException(status_code=422, detail="take_profit does not meet configured minimum risk/reward")
        tp2_rr = max(settings.min_risk_reward + 1.0, tp1_rr + 1.0)
        target_distance = stop_distance * tp2_rr
        tp2_price = alert.price + target_distance if direction == TradeDirection.BUY else alert.price - target_distance
    else:
        tp1_rr = settings.min_risk_reward
        tp2_rr = settings.min_risk_reward + 1.0
        tp1_price, tp2_price = risk.compute_rr_targets(
            alert.price, stop_loss_price, direction,
            rr_tp1=tp1_rr, rr_tp2=tp2_rr,
        )

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
        strategy=StrategyType.NO_TRADE if alert.strategy is None else StrategyType.TREND_PULLBACK,
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
    logger.info("received TradingView alert: %s %s @ %.5f (%s)", alert.ticker, alert.signal, alert.price, alert.timeframe)

    try:
        signal = _alert_to_signal(alert)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("rejected TradingView alert due to invalid risk inputs (%s)", type(exc).__name__)
        raise HTTPException(status_code=422, detail="invalid risk inputs in TradingView alert") from exc

    fingerprint = _alert_fingerprint(alert)
    if not _claim_alert_fingerprint(fingerprint):
        raise HTTPException(status_code=409, detail="duplicate alert recently received")

    # Do not retain the credential after verification; recent-alert inspection
    # only needs the public signal fields.
    _RECENT_ALERTS.append(alert.model_copy(update={"secret": None}))
    try:
        dispatch_results = send_alert(signal)
    except Exception as exc:
        _release_alert_fingerprint(fingerprint)
        logger.error("failed to dispatch alert for %s (%s)", alert.ticker, type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=WebhookAck(status="delivery_failed", detail="alert stored but notification dispatch failed").model_dump(),
        )

    if not isinstance(dispatch_results, dict) or not any(value is True for value in dispatch_results.values()):
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
