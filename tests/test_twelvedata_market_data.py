"""Offline Twelve Data contract tests (mocked HTTP; no live key required)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from src import data_provider


SETTINGS = SimpleNamespace(twelvedata_api_key="unit-test-key", twelvedata_quote_max_age_seconds=90)


@pytest.fixture(autouse=True)
def no_rate_limit_wait(monkeypatch):
    monkeypatch.setattr(data_provider._TWELVEDATA_BUDGET, "reserve", lambda: None)
    data_provider._TWELVEDATA_QUOTE_CACHE.clear()
    data_provider._OHLCV_CACHE.clear()


def test_settings_default_to_twelvedata_and_keep_paper_execution_separate():
    from config import Settings

    settings = Settings(_env_file=None)
    assert settings.data_provider == "twelvedata"
    assert settings.twelvedata_api_key == ""
    assert settings.paper_trading_env == "practice"
    assert settings.enable_paper_trading is False


def test_symbol_normalization_handles_fx_aliases_gold_and_silver():
    normalize = data_provider.normalize_twelvedata_symbol
    assert normalize("EUR_USD") == "EUR/USD"
    assert normalize("EURUSD") == "EUR/USD"
    assert normalize("EURUSD=X") == "EUR/USD"
    assert normalize("FX_IDC:GBPUSD") == "GBP/USD"
    assert normalize("XAU_USD") == "XAU/USD"
    assert normalize("XAUUSD") == "XAU/USD"
    assert normalize("GC=F") == "XAU/USD"
    assert normalize("SI=F") == "XAG/USD"
    with pytest.raises(data_provider.DataFetchError):
        normalize("BTCUSD")


@pytest.mark.parametrize("timeframe,expected", [
    ("15m", "15min"), ("30m", "30min"), ("1h", "1h"),
    ("4h", "4h"), ("1d", "1day"),
])
def test_timeframe_conversion(timeframe, expected):
    assert data_provider._TWELVEDATA_INTERVALS[timeframe] == expected


def _series_values(now: datetime, count: int = 220, timeframe: str = "15m"):
    delta = data_provider._TIMEFRAME_DELTAS[timeframe]
    if timeframe == "15m":
        current_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    elif timeframe == "30m":
        current_start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    elif timeframe == "1h":
        current_start = now.replace(minute=0, second=0, microsecond=0)
    elif timeframe == "4h":
        current_start = now.replace(hour=(now.hour // 4) * 4, minute=0, second=0, microsecond=0)
    else:
        current_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_complete = current_start - delta
    rows = []
    for i in range(count):
        stamp = last_complete - delta * (count - i - 1)
        rows.append({
            "datetime": stamp.strftime("%Y-%m-%d %H:%M:%S"),
            "open": "1.1000", "high": "1.1010", "low": "1.0990",
            "close": "1.1005", "volume": str(50 + i),
        })
    rows.append({
        "datetime": current_start.strftime("%Y-%m-%d %H:%M:%S"),
        "open": "1.1005", "high": "1.1015", "low": "1.1000",
        "close": "1.1010", "volume": "1",
    })
    return rows


def test_time_series_returns_validated_completed_ohlcv_and_skips_open_bar(monkeypatch):
    # Keep the latest complete 15-minute candle inside its freshness window.
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
    url = "https://api.twelvedata.com/time_series"
    payload = {"meta": {"symbol": "EUR/USD"}, "values": _series_values(now)}

    def fake_get(request_url, **kwargs):
        assert request_url == url
        assert kwargs["headers"]["Authorization"] == "apikey unit-test-key"
        assert kwargs["params"] == {
            "symbol": "EUR/USD", "interval": "15min", "outputsize": 220,
            "timezone": "UTC", "order": "ASC",
        }
        assert "apikey" not in str(kwargs["params"])
        return httpx.Response(200, request=httpx.Request("GET", url), json=payload)

    monkeypatch.setattr(data_provider.httpx, "get", fake_get)
    frame = data_provider.fetch_ohlcv_twelvedata(
        data_provider.OHLCVRequest("EUR_USD", "15m", min_bars=210), SETTINGS, now=now
    )
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(frame) == 220
    assert frame.index.tz is not None
    assert frame.index[-1] < pd_timestamp(now)
    assert frame.iloc[-1]["Close"] == pytest.approx(1.1005)
    assert frame.iloc[-1]["Volume"] == 269
    assert frame.attrs["provider"] == "twelvedata"
    assert frame.attrs["price_basis"] == "aggregated_midpoint"
    assert frame.attrs["volume_reported"] is True


def pd_timestamp(value):
    import pandas as pd

    return pd.Timestamp(value)


def test_missing_volume_is_zero_marked_unavailable_not_synthesized(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
    values = _series_values(now)
    for row in values:
        row.pop("volume", None)
    url = "https://api.twelvedata.com/time_series"
    monkeypatch.setattr(data_provider.httpx, "get", lambda *_a, **_k: httpx.Response(
        200, request=httpx.Request("GET", url), json={"values": values}
    ))
    frame = data_provider.fetch_ohlcv_twelvedata(
        data_provider.OHLCVRequest("EURUSD", "15m", min_bars=210), SETTINGS, now=now
    )
    assert (frame["Volume"] == 0).all()
    assert frame.attrs["volume_reported"] is False


def test_empty_series_and_insufficient_bars_are_rejected(monkeypatch):
    url = "https://api.twelvedata.com/time_series"
    payload = {"values": []}
    monkeypatch.setattr(data_provider.httpx, "get", lambda *_a, **_k: httpx.Response(
        200, request=httpx.Request("GET", url), json=payload
    ))
    with pytest.raises(data_provider.DataFetchError, match="no completed candles"):
        data_provider.fetch_ohlcv_twelvedata(data_provider.OHLCVRequest("EURUSD", "1h"), SETTINGS)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
    payload["values"] = _series_values(now, count=10, timeframe="1h")
    with pytest.raises(data_provider.DataFetchError, match="insufficient bars"):
        data_provider.fetch_ohlcv_twelvedata(
            data_provider.OHLCVRequest("EURUSD", "1h", min_bars=210), SETTINGS, now=now
        )


def test_stale_ohlcv_is_rejected(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    values = _series_values(now - timedelta(hours=3), count=220, timeframe="1h")
    url = "https://api.twelvedata.com/time_series"
    monkeypatch.setattr(data_provider.httpx, "get", lambda *_a, **_k: httpx.Response(
        200, request=httpx.Request("GET", url), json={"values": values}
    ))
    with pytest.raises(data_provider.DataFetchError, match="stale"):
        data_provider.fetch_ohlcv_twelvedata(
            data_provider.OHLCVRequest("EURUSD", "1h", min_bars=210), SETTINGS, now=now
        )


@pytest.mark.parametrize("status,pattern", [
    (401, "authentication failed"), (403, "denied access"),
    (429, "request limit reached"), (503, "temporarily unavailable"),
])
def test_http_auth_permission_rate_and_server_errors_are_typed(monkeypatch, status, pattern):
    url = "https://api.twelvedata.com/time_series"
    calls = []

    def fake_get(*_args, **_kwargs):
        calls.append(1)
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(data_provider.httpx, "get", fake_get)
    monkeypatch.setattr(data_provider.time, "sleep", lambda _seconds: None)
    with pytest.raises(data_provider.DataFetchError, match=pattern):
        data_provider._twelvedata_get("/time_series", {"symbol": "EUR/USD"}, SETTINGS)
    assert len(calls) == (2 if status == 503 else 1)


def test_provider_error_payload_and_invalid_json_are_rejected(monkeypatch):
    url = "https://api.twelvedata.com/time_series"
    monkeypatch.setattr(data_provider.httpx, "get", lambda *_a, **_k: httpx.Response(
        200, request=httpx.Request("GET", url), json={"code": 429, "message": "credits depleted", "status": "error"}
    ))
    with pytest.raises(data_provider.DataFetchError, match="credit/rate limit"):
        data_provider._twelvedata_get("/time_series", {}, SETTINGS)
    monkeypatch.setattr(data_provider.httpx, "get", lambda *_a, **_k: httpx.Response(
        200, request=httpx.Request("GET", url), content=b"not-json"
    ))
    with pytest.raises(data_provider.DataFetchError, match="invalid JSON"):
        data_provider._twelvedata_get("/time_series", {}, SETTINGS)


def test_missing_key_fails_closed_without_network(monkeypatch):
    monkeypatch.setattr(data_provider.httpx, "get", lambda *_a, **_k: pytest.fail("network must not be called"))
    with pytest.raises(data_provider.DataFetchError, match="TWELVEDATA_API_KEY"):
        data_provider.fetch_ohlcv_twelvedata(
            data_provider.OHLCVRequest("EURUSD", "1h"),
            SimpleNamespace(twelvedata_api_key=""),
        )


def test_quote_is_a_timestamped_midpoint_with_no_synthetic_bid_ask(monkeypatch):
    now = datetime.now(timezone.utc)
    url = "https://api.twelvedata.com/quote"
    payload = {
        "symbol": "EUR/USD", "close": "1.08510", "last_quote_at": int(now.timestamp()),
        "is_market_open": True,
    }

    def fake_get(request_url, **kwargs):
        assert request_url == url
        assert kwargs["params"] == {"symbol": "EUR/USD", "timezone": "UTC"}
        return httpx.Response(200, request=httpx.Request("GET", url), json=payload)

    monkeypatch.setattr(data_provider.httpx, "get", fake_get)
    quote = data_provider.get_live_quote("EURUSD=X", SETTINGS, use_cache=False)
    data_provider.ensure_live_quote_fresh(quote, 90, now=now)
    assert quote.price_basis == "midpoint"
    assert quote.mid == pytest.approx(1.0851)
    assert quote.bid is None and quote.ask is None
    assert quote.spread is None
    assert quote.observed_at == datetime.fromtimestamp(payload["last_quote_at"], timezone.utc)


def test_closed_market_stale_and_missing_timestamp_quotes_fail_closed(monkeypatch):
    now = datetime.now(timezone.utc)
    quote = data_provider.LiveQuote(
        "EUR/USD", None, None, now, "closed", price_basis="midpoint", midpoint=1.1
    )
    with pytest.raises(data_provider.DataFetchError, match="open-market midpoint"):
        data_provider.ensure_live_quote_fresh(quote, now=now)
    stale = data_provider.LiveQuote(
        "EUR/USD", None, None, now - timedelta(seconds=100), "midpoint",
        price_basis="midpoint", midpoint=1.1,
    )
    with pytest.raises(data_provider.DataFetchError, match="freshness"):
        data_provider.ensure_live_quote_fresh(stale, 90, now=now)
    url = "https://api.twelvedata.com/quote"
    monkeypatch.setattr(data_provider.httpx, "get", lambda *_a, **_k: httpx.Response(
        200, request=httpx.Request("GET", url), json={"close": "1.1", "is_market_open": True}
    ))
    with pytest.raises(data_provider.DataFetchError, match="last_quote_at"):
        data_provider.get_live_quote("EURUSD", SETTINGS, use_cache=False)


def test_fetch_entry_point_skips_provider_errors_without_yfinance_fallback(monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: SimpleNamespace(
        data_provider="twelvedata", twelvedata_api_key="unit-test-key"
    ))
    monkeypatch.setattr(data_provider, "fetch_ohlcv_twelvedata", lambda *_a, **_k: (_ for _ in ()).throw(
        data_provider.DataFetchError("quota exhausted")
    ))
    monkeypatch.setattr(data_provider, "fetch_ohlcv_yfinance", lambda *_a, **_k: pytest.fail("no fallback"))
    assert data_provider.fetch_ohlcv(data_provider.OHLCVRequest("EURUSD", "1h")) is None


def test_protected_market_health_returns_price_basis_without_bid_ask(monkeypatch):
    from fastapi.testclient import TestClient

    from src import tradingview_bridge as bridge

    secret = "market-health-test-secret-at-least-32-characters"
    settings = SimpleNamespace(
        data_provider="twelvedata", twelvedata_quote_max_age_seconds=90,
        webhook_shared_secret=secret,
    )
    quote = data_provider.LiveQuote(
        "EUR/USD", None, None, datetime.now(timezone.utc), "midpoint",
        price_basis="midpoint", midpoint=1.0851,
    )
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)
    monkeypatch.setattr(data_provider, "get_live_quote", lambda *_a, **_k: quote)
    client = TestClient(bridge.app)
    result = client.get("/health/market", headers={"X-Webhook-Secret": secret})
    assert result.status_code == 200
    body = result.json()
    assert body["provider"] == "twelvedata"
    assert body["instrument"] == "EUR/USD"
    assert body["price"] == pytest.approx(1.0851)
    assert body["price_basis"] == "midpoint"
    assert "bid" not in body and "ask" not in body
    assert secret not in result.text
