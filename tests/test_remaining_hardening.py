"""Additional regression tests for resource limits and OANDA paper safety."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
import pandas as pd
from fastapi.testclient import TestClient

from config import Settings
from src import oanda_client, tradingview_bridge as bridge
from src.models import TradeDirection
from src.watchlist_store import WatchlistStore


@pytest.fixture(autouse=True)
def reset_webhook_state():
    with bridge._REQUEST_RATE_LOCK:
        bridge._REQUESTS_BY_CLIENT.clear()
    with bridge._ALERT_DEDUPE_LOCK:
        bridge._SEEN_ALERTS.clear()
    bridge._RECENT_ALERTS.clear()
    yield
    with bridge._REQUEST_RATE_LOCK:
        bridge._REQUESTS_BY_CLIENT.clear()
    with bridge._ALERT_DEDUPE_LOCK:
        bridge._SEEN_ALERTS.clear()
    bridge._RECENT_ALERTS.clear()


def test_webhook_enforces_per_peer_rate_limit(monkeypatch):
    from datetime import datetime, timezone

    from src.data_provider import LiveQuote

    monkeypatch.setattr(bridge, "_REQUEST_RATE_MAX", 2)
    monkeypatch.setattr(bridge, "get_settings", lambda: SimpleNamespace(
        webhook_shared_secret="test-secret-which-is-longer-than-32-chars",
        account_size_usd=10_000.0,
        risk_percent_per_trade=1.0,
        min_risk_reward=2.0,
        data_provider="twelvedata",
        twelvedata_quote_max_age_seconds=30,
    ))
    monkeypatch.setattr(bridge, "send_alert", lambda signal, **_kwargs: {
        "telegram": True, "discord": False, "desktop": False,
    })
    monkeypatch.setattr(
        "src.data_provider.get_live_quote",
        lambda *_args, **_kwargs: LiveQuote(
            "EUR_USD", 1.0849, 1.0851, datetime.now(timezone.utc), "tradeable"
        ),
    )
    monkeypatch.setattr(bridge, "_corroborate_alert", lambda alert: (
        bridge._alert_to_signal(alert),
        None,
        pd.DataFrame(
            {"Open": [alert.price], "High": [alert.price], "Low": [alert.price], "Close": [alert.price], "Volume": [1]},
            index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")]),
        ),
    ))
    client = TestClient(bridge.app)
    base = {
        "ticker": "EURUSD", "timeframe": "15", "signal": "BUY",
        "stop_loss": 1.083, "take_profit": 1.089, "take_profit_2": 1.091,
        "bar_time": int(time.time() * 1000),
        "secret": "test-secret-which-is-longer-than-32-chars",
    }
    def alert_payload(price):
        return {
            **base, "price": price, "stop_loss": price - 0.002,
            "take_profit": price + 0.004, "take_profit_2": price + 0.006,
        }

    responses = [client.post("/webhook/tradingview", json=alert_payload(p))
                 for p in (1.085, 1.0852)]
    blocked = client.post("/webhook/tradingview", json=alert_payload(1.0853))
    assert [r.status_code for r in responses] == [200, 200]
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


@pytest.mark.parametrize(("symbol", "expected"), [
    ("EURUSD", "EUR_USD"),
    ("USDJPY", "USD_JPY"),
    ("EURUSD=X", "EUR_USD"),
    ("XAUUSD", "XAU_USD"),
    ("XAGUSD", "XAG_USD"),
])
def test_oanda_instrument_validation_accepts_currency_pairs(symbol, expected):
    assert oanda_client.to_oanda_instrument(symbol) == expected


@pytest.mark.parametrize("symbol", ["AAPL", "ABCDEF", "BTCUSD", "EURRUR", "EUR/EUR"])
def test_oanda_instrument_validation_rejects_unsupported_symbols(symbol):
    with pytest.raises(oanda_client.PaperTradingConfigError):
        oanda_client.to_oanda_instrument(symbol)


def test_oanda_order_uses_jpy_precision_and_rounds_outward(monkeypatch):
    settings = Settings(
        _env_file=None, paper_account_id="demo-account", paper_api_token="not-a-real-token",
        paper_trading_env="practice",
    )
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, payload=json, headers=headers, timeout=timeout)
        return SimpleNamespace(
            status_code=201,
            json=lambda: {
                "orderCreateTransaction": {"id": "123"},
                "orderFillTransaction": {"price": "150.000", "tradeOpened": {"tradeID": "456"}},
            },
        )

    monkeypatch.setattr(oanda_client.httpx, "post", fake_post)
    result = oanda_client.place_market_order(
        "USDJPY", TradeDirection.BUY, 1000, 149.87654, 150.12341, settings,
    )
    order = captured["payload"]["order"]
    assert result.status == "FILLED"
    assert order["instrument"] == "USD_JPY"
    assert order["stopLossOnFill"]["price"] == "149.876"
    assert order["takeProfitOnFill"]["price"] == "150.124"
    assert captured["url"].startswith("https://api-fxpractice.oanda.com/")
    assert "/v3/accounts/demo-account/orders" in captured["url"]


def test_oanda_order_rejects_zero_units_before_network(monkeypatch):
    settings = Settings(
        _env_file=None, paper_account_id="demo-account", paper_api_token="not-a-real-token",
        paper_trading_env="practice",
    )
    monkeypatch.setattr(oanda_client.httpx, "post", lambda *args, **kwargs: pytest.fail("network must not be called"))
    with pytest.raises(oanda_client.PaperTradingConfigError):
        oanda_client.place_market_order("EURUSD", TradeDirection.BUY, 0, 1.0, 1.1, settings)


def test_watchlist_persistence_uses_complete_json_without_temp_files(tmp_path):
    path = tmp_path / "nested" / "watchlist.json"
    store = WatchlistStore(path=path, defaults=["EURUSD"])
    symbol, was_new = store.add("gold")
    assert (symbol, was_new) == ("XAUUSD", True)
    assert json.loads(path.read_text(encoding="utf-8"))["symbols"] == ["EURUSD", "XAUUSD"]
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
