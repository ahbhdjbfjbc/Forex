"""Offline contract tests for OANDA market data (no broker or order calls)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest

from src import data_provider


SETTINGS = SimpleNamespace(
    oanda_api_token="unit-test-token",
    oanda_account_id="test-account",
    oanda_environment="live",
    oanda_quote_max_age_seconds=30,
)


def test_oanda_symbol_normalization_is_limited_to_fx_and_metals():
    assert data_provider._oanda_instrument("EURUSD=X") == "EUR_USD"
    assert data_provider._oanda_instrument("EUR/USD") == "EUR_USD"
    assert data_provider._oanda_instrument("OANDA:XAUUSD") == "XAU_USD"
    assert data_provider._oanda_instrument("GC=F") == "XAU_USD"
    with pytest.raises(data_provider.DataFetchError):
        data_provider._oanda_instrument("BTCUSD")


def test_oanda_candles_return_only_complete_midpoint_bars(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    last_start = now - timedelta(minutes=15)
    candles = []
    for i in range(220):
        stamp = last_start - timedelta(minutes=15 * (219 - i))
        candles.append({
            "time": stamp.isoformat().replace("+00:00", "Z"),
            "complete": True,
            "volume": 50 + i,
            "mid": {"o": "1.1000", "h": "1.1010", "l": "1.0990", "c": "1.1005"},
        })
    candles.append({
        "time": now.isoformat().replace("+00:00", "Z"),
        "complete": False,
        "volume": 1,
        "mid": {"o": "1.1005", "h": "1.1015", "l": "1.1000", "c": "1.1010"},
    })
    url = "https://api-fxtrade.oanda.com/v3/instruments/EUR_USD/candles"

    def fake_get(request_url, **kwargs):
        assert request_url == url
        assert kwargs["params"]["granularity"] == "M15"
        assert kwargs["params"]["price"] == "M"
        assert "Bearer unit-test-token" == kwargs["headers"]["Authorization"]
        return httpx.Response(200, request=httpx.Request("GET", url), json={"candles": candles})

    monkeypatch.setattr(data_provider.httpx, "get", fake_get)
    frame = data_provider.fetch_ohlcv_oanda(
        data_provider.OHLCVRequest("EURUSD=X", "15m"), SETTINGS
    )
    assert len(frame) == 220
    assert frame.index.tz is not None
    assert frame.iloc[-1]["Close"] == pytest.approx(1.1005)
    assert frame.iloc[-1]["Volume"] == 269


def test_oanda_candles_reject_non_200_and_malformed_payload(monkeypatch):
    url = "https://api-fxtrade.oanda.com/v3/instruments/EUR_USD/candles"
    monkeypatch.setattr(
        data_provider.httpx, "get",
        lambda *_args, **_kwargs: httpx.Response(401, request=httpx.Request("GET", url)),
    )
    with pytest.raises(data_provider.DataFetchError, match="HTTP 401"):
        data_provider.fetch_ohlcv_oanda(data_provider.OHLCVRequest("EURUSD", "1h"), SETTINGS)


def test_oanda_quote_parsing_checks_bid_ask_time_and_trade_status(monkeypatch):
    now = datetime.now(timezone.utc)
    url = "https://api-fxtrade.oanda.com/v3/accounts/test-account/pricing"
    payload = {
        "prices": [{
            "instrument": "EUR_USD",
            "bids": [{"price": "1.08500", "liquidity": "1000000"}],
            "asks": [{"price": "1.08520", "liquidity": "1000000"}],
            "status": "tradeable",
            "time": now.isoformat().replace("+00:00", "Z"),
        }]
    }

    def fake_get(request_url, **kwargs):
        assert request_url == url
        assert kwargs["params"] == {"instruments": "EUR_USD"}
        return httpx.Response(200, request=httpx.Request("GET", url), json=payload)

    monkeypatch.setattr(data_provider.httpx, "get", fake_get)
    data_provider._OANDA_QUOTE_CACHE.clear()
    quote = data_provider.get_oanda_live_quote("EURUSD", SETTINGS, use_cache=False)
    data_provider.ensure_live_quote_fresh(quote, 30, now=now)
    assert quote.bid == pytest.approx(1.085)
    assert quote.ask == pytest.approx(1.0852)
    assert quote.mid == pytest.approx(1.0851)
    assert quote.spread == pytest.approx(0.0002)


def test_stale_quote_is_rejected():
    quote = data_provider.LiveQuote(
        "EUR_USD", 1.0, 1.0001, datetime.now(timezone.utc) - timedelta(seconds=45), "tradeable"
    )
    with pytest.raises(data_provider.DataFetchError, match="freshness"):
        data_provider.ensure_live_quote_fresh(quote, 30)


def test_oanda_credentials_fail_closed_and_never_use_yfinance(monkeypatch):
    settings = SimpleNamespace(
        data_provider="oanda", oanda_api_token="", oanda_account_id="",
        oanda_environment="live",
    )
    monkeypatch.setattr("config.get_settings", lambda: settings)
    monkeypatch.setattr(
        data_provider, "fetch_ohlcv_yfinance",
        lambda *_args, **_kwargs: pytest.fail("must not fall back to Yahoo"),
    )
    assert data_provider.fetch_ohlcv(data_provider.OHLCVRequest("EURUSD", "1h")) is None


def test_reprice_signal_uses_ask_for_buy_and_preserves_distances():
    from config import Settings
    from src.models import TradeDirection
    from src.risk import reprice_signal_to_quote
    from tests.test_signal_lifecycle import make_signal

    settings = Settings(_env_file=None, data_provider="oanda")
    signal = make_signal(entry=1.1000)
    quote = data_provider.LiveQuote(
        "EUR_USD", 1.1001, 1.1002, datetime.now(timezone.utc), "tradeable"
    )
    updated = reprice_signal_to_quote(signal, quote, settings)
    assert updated.direction == TradeDirection.BUY
    assert updated.entry_price == pytest.approx(quote.ask)
    assert updated.stop_loss_price == pytest.approx(1.0902)
    assert updated.take_profit_1_price == pytest.approx(1.1202)
    assert updated.take_profit_2_price == pytest.approx(1.1302)
    assert updated.take_profit_1_rr == pytest.approx(2.0)
    assert updated.take_profit_2_rr == pytest.approx(3.0)


def test_reprice_signal_rejects_large_price_drift():
    from config import Settings
    from src.risk import reprice_signal_to_quote
    from tests.test_signal_lifecycle import make_signal

    settings = Settings(_env_file=None, data_provider="oanda")
    signal = make_signal(entry=1.1000)
    quote = data_provider.LiveQuote(
        "EUR_USD", 1.105, 1.1052, datetime.now(timezone.utc), "tradeable"
    )
    with pytest.raises(ValueError, match="moved"):
        reprice_signal_to_quote(signal, quote, settings)


def test_protected_market_health_returns_only_a_fresh_oanda_quote(monkeypatch):
    from fastapi.testclient import TestClient

    from src import tradingview_bridge as bridge

    secret = "market-health-test-secret-at-least-32-characters"
    settings = SimpleNamespace(
        data_provider="oanda", oanda_quote_max_age_seconds=30,
        webhook_shared_secret=secret,
    )
    quote = data_provider.LiveQuote(
        "EUR_USD", 1.0850, 1.0852, datetime.now(timezone.utc), "tradeable"
    )
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)
    monkeypatch.setattr(
        data_provider, "get_oanda_live_quote", lambda *_a, **_k: quote
    )
    client = TestClient(bridge.app)

    unauthorized = client.get("/health/market", headers={"X-Webhook-Secret": "wrong"})
    result = client.get("/health/market", headers={"X-Webhook-Secret": secret})

    assert unauthorized.status_code == 403
    assert result.status_code == 200
    assert result.json()["provider"] == "oanda"
    assert result.json()["instrument"] == "EUR_USD"
    assert result.json()["bid"] == pytest.approx(1.085)
    assert result.json()["ask"] == pytest.approx(1.0852)
    assert secret not in result.text


def test_protected_market_health_fails_when_live_quote_is_stale(monkeypatch):
    from fastapi.testclient import TestClient

    from src import tradingview_bridge as bridge

    secret = "market-health-test-secret-at-least-32-characters"
    settings = SimpleNamespace(
        data_provider="oanda", oanda_quote_max_age_seconds=30,
        webhook_shared_secret=secret,
    )
    stale = data_provider.LiveQuote(
        "EUR_USD", 1.0850, 1.0852,
        datetime.now(timezone.utc) - timedelta(seconds=60), "tradeable",
    )
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)
    monkeypatch.setattr(data_provider, "get_oanda_live_quote", lambda *_a, **_k: stale)
    response = TestClient(bridge.app).get(
        "/health/market", headers={"X-Webhook-Secret": secret}
    )

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
