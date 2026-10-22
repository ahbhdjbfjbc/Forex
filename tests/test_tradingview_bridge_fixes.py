"""Regression tests for TradingView webhook security and risk math."""
from types import SimpleNamespace

import pytest

from src import tradingview_bridge as bridge
from src.models import TradingViewAlert


@pytest.fixture(autouse=True)
def safe_settings(monkeypatch):
    settings = SimpleNamespace(
        webhook_shared_secret="test-secret-which-is-longer-than-32-chars",
        account_size_usd=10_000.0,
        risk_percent_per_trade=1.0,
        min_risk_reward=2.0,
    )
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)
    bridge._RECENT_ALERTS.clear()
    with bridge._ALERT_DEDUPE_LOCK:
        bridge._SEEN_ALERTS.clear()
    with bridge._REQUEST_RATE_LOCK:
        bridge._REQUESTS_BY_CLIENT.clear()
    yield
    bridge._RECENT_ALERTS.clear()
    with bridge._ALERT_DEDUPE_LOCK:
        bridge._SEEN_ALERTS.clear()
    with bridge._REQUEST_RATE_LOCK:
        bridge._REQUESTS_BY_CLIENT.clear()


def test_recent_alerts_never_return_secret():
    bridge._RECENT_ALERTS.append(TradingViewAlert(
        ticker="EURUSD", timeframe="15", signal="BUY", price=1.085,
        secret="test-secret-which-is-longer-than-32-chars",
    ))
    result = bridge.recent_alerts(x_webhook_secret="test-secret-which-is-longer-than-32-chars")
    assert len(result) == 1
    assert "secret" not in result[0]
    assert result[0]["ticker"] == "EURUSD"


@pytest.mark.parametrize("secret", [
    "change-me-to-a-long-random-string",
    "REPLACE_WITH_A_RANDOM_SECRET_AT_LEAST_32_CHARACTERS",
    "short",
])
def test_default_or_short_shared_secret_fails_closed(monkeypatch, secret):
    monkeypatch.setattr(bridge, "get_settings", lambda: SimpleNamespace(webhook_shared_secret=secret))
    with pytest.raises(Exception) as exc:
        bridge._verify_secret(secret)
    assert getattr(exc.value, "status_code", None) == 403


def test_fx_fallback_stop_and_lot_size_are_pip_based():
    signal = bridge._alert_to_signal(TradingViewAlert(
        ticker="EURUSD", timeframe="15", signal="BUY", price=1.0850,
        secret="test-secret-which-is-longer-than-32-chars",
    ))
    assert signal.stop_loss_price == pytest.approx(1.0830)
    assert signal.stop_loss_pips == pytest.approx(20.0)
    assert signal.risk.lot_size > 0
    assert signal.risk.pip_value_per_lot > 0


@pytest.mark.parametrize("ticker,entry,expected_distance,expected_units", [
    ("XAUUSD", 4300.0, 21.5, 100.0 / 21.5),
    ("BTCUSD", 100000.0, 1000.0, 100.0 / 1000.0),
])
def test_non_fx_fallback_uses_price_distance_and_units(ticker, entry, expected_distance, expected_units):
    signal = bridge._alert_to_signal(TradingViewAlert(
        ticker=ticker, timeframe="15", signal="BUY", price=entry,
        secret="test-secret-which-is-longer-than-32-chars",
    ))
    assert signal.stop_loss_price == pytest.approx(entry - expected_distance)
    assert signal.stop_loss_pips == pytest.approx(expected_distance)
    assert signal.risk.pip_value_per_lot == 0.0
    assert signal.risk.lot_size == pytest.approx(expected_units, rel=1e-3)
    assert signal.take_profit_1_price > entry


def test_wrong_side_stop_is_rejected():
    with pytest.raises(Exception) as exc:
        bridge._alert_to_signal(TradingViewAlert(
            ticker="XAUUSD", timeframe="15", signal="BUY", price=4300.0,
            stop_loss=4310.0, secret="test-secret-which-is-longer-than-32-chars",
        ))
    assert getattr(exc.value, "status_code", None) == 422


def test_http_recent_endpoint_does_not_expose_secret():
    from fastapi.testclient import TestClient
    client = TestClient(bridge.app)
    bridge._RECENT_ALERTS.append(TradingViewAlert(
        ticker="BTCUSD", timeframe="15", signal="BUY", price=100000,
        secret="test-secret-which-is-longer-than-32-chars",
    ))
    response = client.get("/webhook/recent", headers={
        "X-Webhook-Secret": "test-secret-which-is-longer-than-32-chars"
    })
    assert response.status_code == 200
    assert "secret" not in response.text


def test_webhook_rejects_wrong_side_stop_without_storing_alert(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(bridge, "send_alert", lambda signal: None)
    client = TestClient(bridge.app)
    response = client.post("/webhook/tradingview", json={
        "ticker": "XAUUSD", "timeframe": "15", "signal": "BUY", "price": 4300,
        "stop_loss": 4310, "secret": "test-secret-which-is-longer-than-32-chars",
    })
    assert response.status_code == 422
    assert len(bridge._RECENT_ALERTS) == 0


def test_webhook_returns_multistatus_when_no_channel_reports_delivery(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(bridge, "send_alert", lambda signal: {
        "telegram": False, "discord": False, "desktop": False,
    })
    client = TestClient(bridge.app)
    response = client.post("/webhook/tradingview", json={
        "ticker": "EURUSD", "timeframe": "15", "signal": "BUY", "price": 1.085,
        "secret": "test-secret-which-is-longer-than-32-chars",
    })
    assert response.status_code == 503
    assert response.json()["status"] == "delivery_failed"
    assert "no notification channel reported delivery" in response.json()["detail"]


def test_webhook_reports_success_when_a_channel_reports_delivery(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(bridge, "send_alert", lambda signal: {
        "telegram": True, "discord": False, "desktop": False,
    })
    client = TestClient(bridge.app)
    response = client.post("/webhook/tradingview", json={
        "ticker": "EURUSD", "timeframe": "15", "signal": "BUY", "price": 1.085,
        "secret": "test-secret-which-is-longer-than-32-chars",
    })
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_webhook_rejects_exact_short_window_replay(monkeypatch):
    from fastapi.testclient import TestClient
    calls = []
    monkeypatch.setattr(bridge, "send_alert", lambda signal: calls.append(signal) or {
        "telegram": True, "discord": False, "desktop": False,
    })
    client = TestClient(bridge.app)
    payload = {
        "ticker": "EURUSD", "timeframe": "15", "signal": "BUY", "price": 1.085,
        "secret": "test-secret-which-is-longer-than-32-chars",
    }
    first = client.post("/webhook/tradingview", json=payload)
    second = client.post("/webhook/tradingview", json=payload)
    assert first.status_code == 200
    assert second.status_code == 409
    assert len(calls) == 1


def test_explicit_take_profit_is_validated_and_used():
    signal = bridge._alert_to_signal(TradingViewAlert(
        ticker="EURUSD", timeframe="15", signal="BUY", price=1.085,
        stop_loss=1.083, take_profit=1.091,
        secret="test-secret-which-is-longer-than-32-chars",
    ))
    assert signal.take_profit_1_price == pytest.approx(1.091)
    assert signal.take_profit_1_rr == pytest.approx(3.0)
    assert signal.take_profit_2_price > signal.take_profit_1_price


def test_sell_rejects_negative_computed_take_profit():
    with pytest.raises(Exception) as exc:
        bridge._alert_to_signal(TradingViewAlert(
            ticker="EURUSD", timeframe="15", signal="SELL", price=0.01,
            stop_loss=0.02,
            secret="test-secret-which-is-longer-than-32-chars",
        ))
    assert getattr(exc.value, "status_code", None) == 422


def test_recent_endpoint_requires_secret():
    from fastapi.testclient import TestClient
    response = TestClient(bridge.app).get("/webhook/recent")
    assert response.status_code == 403


def test_unsupported_ticker_is_not_misclassified_for_fx_risk_sizing():
    with pytest.raises(Exception) as exc:
        bridge._alert_to_signal(TradingViewAlert(
            ticker="AAPL", timeframe="15", signal="BUY", price=250.0,
            secret="test-secret-which-is-longer-than-32-chars",
        ))
    assert getattr(exc.value, "status_code", None) == 422


def test_unknown_six_character_ticker_is_not_misclassified_as_fx():
    with pytest.raises(Exception) as exc:
        bridge._alert_to_signal(TradingViewAlert(
            ticker="ABCUSD", timeframe="15", signal="BUY", price=1.0,
            secret="test-secret-which-is-longer-than-32-chars",
        ))
    assert getattr(exc.value, "status_code", None) == 422
