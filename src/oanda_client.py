"""Thin REST wrapper around OANDA's v20 API — practice/demo accounts only.

This module is hard-locked to OANDA's practice endpoint (`_PRACTICE_BASE_URL`)
and refuses to run if `PAPER_TRADING_ENV` is set to anything other than
`"practice"` — it never even constructs a live-money URL. This whole module
exists specifically for paper/simulated trading; that guard is what keeps it
that way even if a `.env` gets copy-pasted somewhere it shouldn't.

Instrument format: OANDA uses underscore-separated instruments ("EUR_USD",
"XAU_USD"). Crypto generally isn't offered on OANDA practice accounts, so
`to_oanda_instrument` raises for those — this bridge only supports FX +
metals.
"""
from __future__ import annotations

import logging
import math
import re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from dataclasses import dataclass
from typing import Any

import httpx

from config import Settings, get_settings
from src.models import TradeDirection
from src.symbol_resolver import instrument_type

logger = logging.getLogger(__name__)

_PRACTICE_BASE_URL = "https://api-fxpractice.oanda.com"
_REQUEST_TIMEOUT = 15.0
# Currency whitelist prevents arbitrary six-character tickers from becoming
# syntactically plausible OANDA instrument names. The broker remains the final
# authority on which specific crosses are enabled for the configured account.
_SUPPORTED_OANDA_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "CNH",
    "HKD", "SGD", "MXN", "ZAR", "TRY", "NOK", "SEK", "DKK", "PLN", "CZK",
    "HUF", "ILS", "THB", "INR", "KRW", "PHP", "IDR", "MYR", "TWD", "BRL",
    "RUB", "AED", "SAR", "QAR", "KWD", "BHD", "OMR", "CLP", "COP", "PEN",
    "ARS", "RON", "ISK",
})
_SUPPORTED_OANDA_METALS = frozenset({"XAUUSD", "XAGUSD"})


class PaperTradingConfigError(Exception):
    pass


class PaperTradingRequestError(Exception):
    pass


@dataclass(frozen=True)
class OandaOrderResult:
    order_id: str | None
    trade_id: str | None
    fill_price: float | None
    status: str
    raw: dict[str, Any]


def _base_url(settings: Settings) -> str:
    if settings.paper_trading_env.strip().lower() != "practice":
        raise PaperTradingConfigError(
            "PAPER_TRADING_ENV must be 'practice' — this module only ever talks to "
            "OANDA's practice/demo endpoint and refuses to construct a live-money URL."
        )
    return _PRACTICE_BASE_URL


def _headers(settings: Settings) -> dict[str, str]:
    if not settings.paper_api_token:
        raise PaperTradingConfigError("PAPER_API_TOKEN is not set")
    return {"Authorization": f"Bearer {settings.paper_api_token}", "Content-Type": "application/json"}


def _require_account_id(settings: Settings) -> str:
    if not settings.paper_account_id:
        raise PaperTradingConfigError("PAPER_ACCOUNT_ID is not set")
    return settings.paper_account_id


def to_oanda_instrument(symbol: str) -> str:
    """Validate a supported currency pair and map it to OANDA's format."""
    normalized = symbol.upper().replace("=X", "").replace("/", "").strip()
    if not re.fullmatch(r"[A-Z]{6}", normalized):
        raise PaperTradingConfigError(f"can't map {symbol!r} to an OANDA instrument")
    if normalized in _SUPPORTED_OANDA_METALS:
        return f"{normalized[:3]}_{normalized[3:]}"
    base, quote = normalized[:3], normalized[3:]
    if base == quote or base not in _SUPPORTED_OANDA_CURRENCIES or quote not in _SUPPORTED_OANDA_CURRENCIES:
        raise PaperTradingConfigError(f"unsupported OANDA currency pair ({normalized})")
    if instrument_type(normalized) != "FX":
        raise PaperTradingConfigError(f"OANDA paper trading does not support this instrument ({normalized})")
    return f"{base}_{quote}"


def _response_object(resp: Any, context: str) -> dict[str, Any]:
    """Parse a JSON object without surfacing response bodies or credentials."""
    try:
        body = resp.json()
    except (ValueError, TypeError) as exc:
        raise PaperTradingRequestError(
            f"OANDA {context} returned invalid JSON (HTTP {resp.status_code})"
        ) from exc
    if not isinstance(body, dict):
        raise PaperTradingRequestError(
            f"OANDA {context} returned an unexpected response shape (HTTP {resp.status_code})"
        )
    return body


def get_account_summary(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    account_id = _require_account_id(settings)
    url = f"{_base_url(settings)}/v3/accounts/{account_id}/summary"
    try:
        resp = httpx.get(url, headers=_headers(settings), timeout=_REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        raise PaperTradingRequestError(f"OANDA account summary request failed ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise PaperTradingRequestError(f"OANDA account summary failed (HTTP {resp.status_code})")
    body = _response_object(resp, "account summary")
    if not isinstance(body.get("account"), dict):
        raise PaperTradingRequestError("OANDA account summary has no account object")
    return body


def get_open_trades(settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    account_id = _require_account_id(settings)
    url = f"{_base_url(settings)}/v3/accounts/{account_id}/openTrades"
    try:
        resp = httpx.get(url, headers=_headers(settings), timeout=_REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        raise PaperTradingRequestError(f"OANDA open trades request failed ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise PaperTradingRequestError(f"OANDA open trades failed (HTTP {resp.status_code})")
    body = _response_object(resp, "open trades")
    trades = body.get("trades", [])
    if not isinstance(trades, list) or any(not isinstance(trade, dict) for trade in trades):
        raise PaperTradingRequestError("OANDA open trades has an invalid trades list")
    return trades


def place_market_order(
    symbol: str,
    direction: TradeDirection,
    units: int,
    stop_loss_price: float,
    take_profit_price: float,
    settings: Settings | None = None,
) -> OandaOrderResult:
    """Places a market order with attached SL/TP on the practice account."""
    settings = settings or get_settings()
    account_id = _require_account_id(settings)
    instrument = to_oanda_instrument(symbol)
    if not isinstance(direction, TradeDirection):
        raise PaperTradingConfigError("direction must be a TradeDirection value")
    if not isinstance(units, int) or isinstance(units, bool) or units <= 0:
        raise PaperTradingConfigError("units must be a positive integer")
    if not all(math.isfinite(value) and value > 0 for value in (stop_loss_price, take_profit_price)):
        raise PaperTradingConfigError("stop-loss and take-profit prices must be finite and positive")
    if direction == TradeDirection.BUY and stop_loss_price >= take_profit_price:
        raise PaperTradingConfigError("BUY stop-loss must be below take-profit")
    if direction == TradeDirection.SELL and stop_loss_price <= take_profit_price:
        raise PaperTradingConfigError("SELL stop-loss must be above take-profit")
    signed_units = units if direction == TradeDirection.BUY else -units
    # OANDA FX instruments quoted in JPY generally use three decimal places;
    # other common FX pairs use five. Broker metadata remains authoritative.
    # Round outward so precision conversion cannot tighten a stop or pull a
    # target closer to entry: BUY SL down / TP up, SELL SL up / TP down.
    price_precision = 3 if instrument.endswith("_JPY") or instrument.startswith(("XAU_", "XAG_")) else 5
    quantum = Decimal(1).scaleb(-price_precision)
    stop_rounding = ROUND_FLOOR if direction == TradeDirection.BUY else ROUND_CEILING
    target_rounding = ROUND_CEILING if direction == TradeDirection.BUY else ROUND_FLOOR
    formatted_stop = format(
        Decimal(str(stop_loss_price)).quantize(quantum, rounding=stop_rounding),
        f".{price_precision}f",
    )
    formatted_target = format(
        Decimal(str(take_profit_price)).quantize(quantum, rounding=target_rounding),
        f".{price_precision}f",
    )

    order_payload = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(signed_units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": formatted_stop},
            "takeProfitOnFill": {"price": formatted_target},
        }
    }
    url = f"{_base_url(settings)}/v3/accounts/{account_id}/orders"

    try:
        resp = httpx.post(url, headers=_headers(settings), json=order_payload, timeout=_REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        raise PaperTradingRequestError(f"OANDA order request failed ({type(exc).__name__})") from exc

    try:
        body = resp.json()
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    if resp.status_code not in (200, 201):
        error_code = body.get("errorCode")
        reject_reason = body.get("orderRejectTransaction", {})
        if isinstance(reject_reason, dict):
            reject_reason = reject_reason.get("reason")
        safe_reason = error_code or reject_reason
        if not isinstance(safe_reason, str) or not re.fullmatch(r"[A-Z0-9_ -]{1,80}", safe_reason):
            safe_reason = "request rejected"
        raise PaperTradingRequestError(f"OANDA order rejected (HTTP {resp.status_code}; {safe_reason})")

    create_tx = body.get("orderCreateTransaction", {})
    if not isinstance(create_tx, dict):
        create_tx = {}

    fill = body.get("orderFillTransaction")
    if fill is not None:
        if not isinstance(fill, dict):
            raise PaperTradingRequestError("OANDA order response has an invalid fill transaction")
        trade_opened = fill.get("tradeOpened") or {}
        if not isinstance(trade_opened, dict):
            raise PaperTradingRequestError("OANDA order response has an invalid opened-trade object")
        try:
            fill_price = float(fill["price"]) if "price" in fill else None
        except (TypeError, ValueError):
            raise PaperTradingRequestError("OANDA order response has an invalid fill price")
        if fill_price is not None and (not math.isfinite(fill_price) or fill_price <= 0):
            raise PaperTradingRequestError("OANDA order response has an invalid fill price")
        return OandaOrderResult(
            order_id=create_tx.get("id"),
            trade_id=trade_opened.get("tradeID"),
            fill_price=fill_price,
            status="FILLED",
            raw=body,
        )

    cancel = body.get("orderCancelTransaction")
    if not isinstance(cancel, dict):
        raise PaperTradingRequestError("OANDA order response contains neither a fill nor a cancellation")
    reason = cancel.get("reason", "UNKNOWN")
    if not isinstance(reason, str) or not re.fullmatch(r"[A-Z0-9_ -]{1,80}", reason):
        reason = "UNKNOWN"
    return OandaOrderResult(
        order_id=create_tx.get("id"),
        trade_id=None,
        fill_price=None,
        status=reason,
        raw=body,
    )
