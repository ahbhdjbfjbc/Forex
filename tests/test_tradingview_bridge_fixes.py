"""Regression tests for TradingView webhook security and risk math."""
from types import SimpleNamespace
import time

import pandas as pd
import pytest

from src import tradingview_bridge as bridge
from src.models import TradingViewAlert


def _fresh_bar_time():
    return int(time.time() * 1000)


def _mock_corroboration(alert):
    if alert.signal == "NONE":
        signal = None
    else:
        signal = bridge._alert_to_signal(alert)
    frame = pd.DataFrame(
        {"Open": [alert.price], "High": [alert.price], "Low": [alert.price], "Close": [alert.price], "Volume": [1]},
        index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")]),
    )
    return signal, None, frame


@pytest.fixture(autouse=True)
def safe_settings(monkeypatch):
    from datetime import datetime, timezone

    from src.data_provider import LiveQuote

    settings = SimpleNamespace(
        webhook_shared_secret="test-secret-which-is-longer-than-32-chars",
        account_size_usd=10_000.0,
        risk_percent_per_trade=1.0,
        min_risk_reward=2.0,
        data_provider="oanda",
        oanda_quote_max_age_seconds=30,
    )
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "src.data_provider.get_oanda_live_quote",
        lambda symbol, *_args, **_kwargs: LiveQuote(
            "EUR_USD" if "EUR" in symbol else "XAU_USD",
            1.0849 if "EUR" in symbol else 4299.9,
            1.0851 if "EUR" in symbol else 4300.1,
            datetime.now(timezone.utc), "tradeable",
        ),
    )
    monkeypatch.setattr(bridge, "_corroborate_alert", _mock_corroboration)
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


def test_explicit_stop_and_targets_use_pip_based_lot_size():
    signal = bridge._alert_to_signal(TradingViewAlert(
        ticker="EURUSD", timeframe="15", signal="BUY", price=1.0850,
        stop_loss=1.0830, take_profit=1.0910, take_profit_2=1.0970,
        secret="test-secret-which-is-longer-than-32-chars",
    ))
    assert signal.stop_loss_price == pytest.approx(1.0830)
    assert signal.stop_loss_pips == pytest.approx(20.0)
    assert signal.risk.lot_size > 0
    assert signal.risk.pip_value_per_lot > 0


def test_pine_levels_are_rebased_to_fresh_oanda_ask_without_default_levels(monkeypatch):
    from datetime import datetime, timezone

    from src.data_provider import LiveQuote
    from tests.test_signal_lifecycle import make_signal

    alert = TradingViewAlert(
        ticker="EURUSD", timeframe="15", signal="BUY", price=1.0850,
        stop_loss=1.0830, take_profit=1.0890, take_profit_2=1.0910,
        secret="test-secret-which-is-longer-than-32-chars",
    )
    signal = make_signal(entry=1.0850)
    quote = LiveQuote("EUR_USD", 1.0853, 1.0855, datetime.now(timezone.utc), "tradeable")
    monkeypatch.setattr("src.data_provider.get_oanda_live_quote", lambda *_a, **_k: quote)

    adjusted = bridge._apply_tradingview_levels(signal, alert, bridge.get_settings())

    assert adjusted.entry_price == pytest.approx(1.0855)
    assert adjusted.stop_loss_price == pytest.approx(1.0835)
    assert adjusted.take_profit_1_price == pytest.approx(1.0895)
    assert adjusted.take_profit_2_price == pytest.approx(1.0915)
    assert adjusted.take_profit_1_rr == pytest.approx(2.0)
    assert adjusted.take_profit_2_rr == pytest.approx(3.0)


def test_pine_sell_levels_are_rebased_to_fresh_oanda_bid(monkeypatch):
    from datetime import datetime, timezone

    from src.data_provider import LiveQuote
    from src.models import TradeDirection
    from tests.test_signal_lifecycle import make_signal

    alert = TradingViewAlert(
        ticker="EURUSD", timeframe="15", signal="SELL", price=1.0850,
        stop_loss=1.0870, take_profit=1.0810, take_profit_2=1.0790,
        secret="test-secret-which-is-longer-than-32-chars",
    )
    signal = make_signal(entry=1.0850, direction=TradeDirection.SELL)
    quote = LiveQuote("EUR_USD", 1.0845, 1.0847, datetime.now(timezone.utc), "tradeable")
    monkeypatch.setattr("src.data_provider.get_oanda_live_quote", lambda *_a, **_k: quote)

    adjusted = bridge._apply_tradingview_levels(signal, alert, bridge.get_settings())

    assert adjusted.entry_price == pytest.approx(1.0845)
    assert adjusted.stop_loss_price == pytest.approx(1.0865)
    assert adjusted.take_profit_1_price == pytest.approx(1.0805)
    assert adjusted.take_profit_2_price == pytest.approx(1.0785)
    assert adjusted.take_profit_1_rr == pytest.approx(2.0)
    assert adjusted.take_profit_2_rr == pytest.approx(3.0)


def test_pine_webhook_contract_serializes_explicit_stop_and_two_targets():
    from pathlib import Path

    pine = Path(__file__).resolve().parents[1] / "pine_script" / "tv_indicator.pine"
    source = pine.read_text(encoding="utf-8")

    assert '"stop_loss":' in source
    assert '"take_profit":' in source
    assert '"take_profit_2":' in source
    assert "longSL  = close - atrVal * slAtrMultiplier" in source
    assert "shortSL  = close + atrVal * slAtrMultiplier" in source
    assert "alert(longMessage, alert.freq_once_per_bar_close)" in source
    assert "alert(shortMessage, alert.freq_once_per_bar_close)" in source


@pytest.mark.parametrize("ticker,entry,expected_distance,expected_units", [
    ("XAUUSD", 4300.0, 21.5, 100.0 / 21.5),
    ("BTCUSD", 100000.0, 1000.0, 100.0 / 1000.0),
])
def test_missing_tradingview_levels_are_rejected_without_defaults(ticker, entry, expected_distance, expected_units):
    with pytest.raises(Exception) as exc:
        bridge._alert_to_signal(TradingViewAlert(
            ticker=ticker, timeframe="15", signal="BUY", price=entry,
            secret="test-secret-which-is-longer-than-32-chars",
        ))
    assert getattr(exc.value, "status_code", None) == 422


def test_wrong_side_stop_is_rejected():
    with pytest.raises(Exception) as exc:
        bridge._alert_to_signal(TradingViewAlert(
            ticker="XAUUSD", timeframe="15", signal="BUY", price=4300.0,
            stop_loss=4310.0, take_profit=4320.0, take_profit_2=4330.0,
            secret="test-secret-which-is-longer-than-32-chars",
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
    monkeypatch.setattr(bridge, "send_alert", lambda signal, **_kwargs: None)
    client = TestClient(bridge.app)
    response = client.post("/webhook/tradingview", json={
        "ticker": "XAUUSD", "timeframe": "15", "signal": "BUY", "price": 4300,
        "stop_loss": 4310, "take_profit": 4320, "take_profit_2": 4330,
        "bar_time": _fresh_bar_time(),
        "secret": "test-secret-which-is-longer-than-32-chars",
    })
    assert response.status_code == 422
    assert len(bridge._RECENT_ALERTS) == 0


def test_webhook_returns_multistatus_when_no_channel_reports_delivery(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(bridge, "send_alert", lambda signal, **_kwargs: {
        "telegram": False, "discord": False, "desktop": False,
    })
    client = TestClient(bridge.app)
    response = client.post("/webhook/tradingview", json={
        "ticker": "EURUSD", "timeframe": "15", "signal": "BUY", "price": 1.085,
        "stop_loss": 1.083, "take_profit": 1.089, "take_profit_2": 1.091,
        "bar_time": _fresh_bar_time(),
        "secret": "test-secret-which-is-longer-than-32-chars",
    })
    assert response.status_code == 503
    assert response.json()["status"] == "delivery_failed"
    assert "required Telegram chart post" in response.json()["detail"]


def test_webhook_reports_success_when_a_channel_reports_delivery(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(bridge, "send_alert", lambda signal, **_kwargs: {
        "telegram": True, "discord": False, "desktop": False,
    })
    client = TestClient(bridge.app)
    response = client.post("/webhook/tradingview", json={
        "ticker": "EURUSD", "timeframe": "15", "signal": "BUY", "price": 1.085,
        "stop_loss": 1.083, "take_profit": 1.089, "take_profit_2": 1.091,
        "bar_time": _fresh_bar_time(),
        "secret": "test-secret-which-is-longer-than-32-chars",
    })
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_webhook_rejects_exact_short_window_replay(monkeypatch):
    from fastapi.testclient import TestClient
    calls = []
    monkeypatch.setattr(bridge, "send_alert", lambda signal, **_kwargs: calls.append(signal) or {
        "telegram": True, "discord": False, "desktop": False,
    })
    client = TestClient(bridge.app)
    payload = {
        "ticker": "EURUSD", "timeframe": "15", "signal": "BUY", "price": 1.085,
        "stop_loss": 1.083, "take_profit": 1.089, "take_profit_2": 1.091,
        "bar_time": _fresh_bar_time(),
        "secret": "test-secret-which-is-longer-than-32-chars",
    }
    first = client.post("/webhook/tradingview", json=payload)
    second = client.post("/webhook/tradingview", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate_suppressed"
    assert len(calls) == 1


def test_webhook_suppresses_active_recommendation_and_reports_tp2_once(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from config import Settings
    from src import charting, notifier
    from src.data_provider import LiveQuote
    from src.signal_lifecycle import SignalLifecycleStore

    lifecycle = SignalLifecycleStore(tmp_path / "signal-state.sqlite3")
    settings = Settings(
        _env_file=None, min_risk_reward=2.0,
        telegram_bot_token="1:test", telegram_channel_id="-100testchannel",
    )
    messages = []
    live = {"entry": 1.085}
    monkeypatch.setattr(
        "src.data_provider.get_oanda_live_quote",
        lambda *_args, **_kwargs: LiveQuote(
            "EUR_USD", live["entry"] - 0.0001, live["entry"],
            datetime.now(timezone.utc), "tradeable",
        ),
    )
    monkeypatch.setattr(bridge, "signal_lifecycle", lifecycle)
    monkeypatch.setattr(notifier, "signal_lifecycle", lifecycle)
    monkeypatch.setattr(notifier, "get_settings", lambda: settings)
    monkeypatch.setattr(bridge, "send_alert", notifier.send_alert)
    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()

    def fake_chart(_signal, _frame, signal_id):
        image_path = chart_dir / f"{signal_id}.png"
        source_path = chart_dir / f"{signal_id}.json"
        image_path.write_bytes(b"chart")
        source_path.write_text("{}", encoding="utf-8")
        return charting.ChartAssets(image_path, source_path)

    monkeypatch.setattr(charting, "create_signal_chart", fake_chart)
    def fake_dispatch_signal(message, _image, _settings=None):
        messages.append(message)
        notifier._delivery_state.last_photo_message_id = 777
        notifier._delivery_state.last_photo_chat_id = "-100testchannel"
        return {"telegram": True, "discord": False, "desktop": False}

    monkeypatch.setattr(notifier, "dispatch_signal", fake_dispatch_signal)
    monkeypatch.setattr(
        notifier, "send_telegram",
        lambda message, *_a, **_k: messages.append(message) or True,
    )
    client = TestClient(bridge.app)
    base = {
        "ticker": "EURUSD", "timeframe": "15", "signal": "BUY",
        "bar_time": _fresh_bar_time(),
        "secret": "test-secret-which-is-longer-than-32-chars",
    }

    def alert_payload(price):
        return {
            **base,
            "price": price,
            "stop_loss": price - 0.002,
            "take_profit": price + 0.004,
            "take_profit_2": price + 0.006,
        }

    live["entry"] = 1.085
    first = client.post("/webhook/tradingview", json=alert_payload(1.085))
    live["entry"] = 1.086
    repeated = client.post("/webhook/tradingview", json=alert_payload(1.086))
    live["entry"] = 1.092
    closed = client.post("/webhook/tradingview", json=alert_payload(1.092))
    still_blocked = client.post("/webhook/tradingview", json=alert_payload(1.093))

    assert first.json()["status"] == "ok"
    assert repeated.json()["status"] == "duplicate_suppressed"
    assert closed.json()["status"] == "duplicate_suppressed"
    assert still_blocked.json()["status"] == "live_quote_unavailable"
    assert len(messages) == 2
    assert "توصية تداول احترافية جديدة" in messages[0]
    assert "نتيجة التوصية" in messages[1]
    assert "تحقق الهدف" in messages[1]

    reset = client.post(
        "/webhook/tradingview",
        json={**base, "signal": "NONE", "price": 1.093, "bar_time": _fresh_bar_time()},
    )
    live["entry"] = 1.094
    next_setup = client.post(
        "/webhook/tradingview", json={**alert_payload(1.094), "bar_time": _fresh_bar_time()}
    )
    assert reset.json()["status"] == "reset"
    assert next_setup.json()["status"] == "ok"
    assert len(messages) == 3


def test_tradingview_bar_time_accepts_unix_milliseconds():
    alert = TradingViewAlert(
        ticker="EURUSD", timeframe="15", signal="BUY", price=1.085,
        bar_time=1710000000000,
        secret="test-secret-which-is-longer-than-32-chars",
    )
    assert alert.bar_time == "1710000000000"


def test_explicit_take_profit_is_validated_and_used():
    signal = bridge._alert_to_signal(TradingViewAlert(
        ticker="EURUSD", timeframe="15", signal="BUY", price=1.085,
        stop_loss=1.083, take_profit=1.091, take_profit_2=1.097,
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
