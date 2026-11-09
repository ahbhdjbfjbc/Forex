"""Offline tests for source-data, calendar, and TradingView timestamp gates."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest
from fastapi import HTTPException

from src import data_provider, news_filter, tradingview_bridge
from src.models import StrategyType, TradingViewAlert


@pytest.fixture(autouse=True)
def reset_news_cache():
    with news_filter._fetch_lock:
        news_filter._cached_events = None
        news_filter._last_fetch_time = None
    yield
    with news_filter._fetch_lock:
        news_filter._cached_events = None
        news_filter._last_fetch_time = None


def _bars(now=None):
    stamp = pd.Timestamp(now or datetime.now(timezone.utc))
    return pd.DataFrame(
        {
            "Open": [1.0], "High": [1.2], "Low": [0.9],
            "Close": [1.1], "Volume": [10],
        },
        index=pd.DatetimeIndex([stamp]),
    )


def test_market_data_validator_rejects_impossible_ohlc():
    request = data_provider.OHLCVRequest("EURUSD=X", "15m", min_bars=1)
    bad = _bars()
    bad.loc[bad.index[0], "High"] = 0.95
    with pytest.raises(data_provider.DataFetchError, match="impossible bars"):
        data_provider._validate(bad, request)


def test_market_data_freshness_rejects_stale_and_future_bars():
    request = data_provider.OHLCVRequest("EURUSD=X", "15m", min_bars=1)
    now = datetime.now(timezone.utc)
    with pytest.raises(data_provider.DataFetchError, match="stale"):
        data_provider._ensure_fresh(_bars(now - timedelta(hours=2)), request, now=now)
    with pytest.raises(data_provider.DataFetchError, match="future"):
        data_provider._ensure_fresh(_bars(now + timedelta(minutes=10)), request, now=now)


def test_market_data_provider_unknown_name_fails_closed(monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: SimpleNamespace(data_provider="other-feed"))
    monkeypatch.setattr(data_provider, "fetch_ohlcv_yfinance", lambda _request: pytest.fail("must not call yfinance"))
    assert data_provider.fetch_ohlcv(data_provider.OHLCVRequest("EURUSD=X", "15m")) is None


def test_provider_sdk_error_is_contained(monkeypatch):
    monkeypatch.setattr("config.get_settings", lambda: SimpleNamespace(data_provider="yfinance"))
    monkeypatch.setattr(data_provider, "fetch_ohlcv_yfinance", lambda _request: (_ for _ in ()).throw(ImportError()))
    assert data_provider.fetch_ohlcv(data_provider.OHLCVRequest("EURUSD=X", "15m")) is None


def test_news_feed_unavailable_pauses_recommendations(monkeypatch):
    monkeypatch.setattr(news_filter.httpx, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    status = news_filter.check_news_status(SimpleNamespace(enable_news_filter=True, news_buffer_minutes=30))
    assert status.is_paused is True
    assert status.data_available is False
    assert "إيقاف التوصيات" in status.details


def test_valid_empty_news_calendar_does_not_pause(monkeypatch):
    monkeypatch.setattr(news_filter.httpx, "get", lambda *_args, **_kwargs: httpx.Response(
        200, request=httpx.Request("GET", news_filter.CALENDAR_URL), json=[]
    ))
    status = news_filter.check_news_status(SimpleNamespace(enable_news_filter=True, news_buffer_minutes=30))
    assert status.is_paused is False
    assert status.data_available is True


def test_active_high_impact_news_pauses(monkeypatch):
    event = {
        "impact": "High", "country": "USD", "title": "Rate announcement",
        "date": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    monkeypatch.setattr(news_filter.httpx, "get", lambda *_args, **_kwargs: httpx.Response(
        200, request=httpx.Request("GET", news_filter.CALENDAR_URL), json=[event]
    ))
    status = news_filter.check_news_status(SimpleNamespace(enable_news_filter=True, news_buffer_minutes=30))
    assert status.is_paused is True
    assert "Rate announcement" in status.details


def test_malformed_high_impact_event_date_fails_closed(monkeypatch):
    event = {"impact": "High", "country": "USD", "title": "Rate announcement", "date": "not-a-date"}
    monkeypatch.setattr(news_filter, "_fetch_calendar_events", lambda: [event])

    status = news_filter.check_news_status(
        SimpleNamespace(enable_news_filter=True, news_buffer_minutes=30)
    )

    assert status.is_paused is True
    assert status.data_available is False
    assert "غير مكتملة" in status.details


def test_news_gate_monitor_fails_closed_on_unexpected_error(monkeypatch, tmp_path):
    from src import storage
    import main
    settings = SimpleNamespace(enable_news_filter=True)
    monkeypatch.setattr(storage, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "report_health", lambda *_args: False)
    monkeypatch.setattr(main, "check_news_status", lambda _settings: (_ for _ in ()).throw(RuntimeError("feed down")))
    monkeypatch.setattr(main, "send_news_pause_alert", lambda *_args: None)
    monkeypatch.setattr(main, "send_news_resume_alert", lambda *_args: None)
    monkeypatch.setattr(main, "_is_paused_for_news", False)
    monkeypatch.setattr(main, "_scan_deferred_by_news", False)
    assert main.monitor_news_state(settings) is True


def test_news_pause_notice_is_once_per_transition_even_after_state_reload(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from src import storage
    import main

    monkeypatch.setattr(storage, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "report_health", lambda *_args: False)
    settings = SimpleNamespace(enable_news_filter=True)
    unavailable = SimpleNamespace(
        is_paused=True,
        data_available=False,
        details="تعذر التحقق من تقويم الأخبار؛ تم إيقاف التوصيات مؤقتاً احترازياً.",
    )
    healthy = SimpleNamespace(is_paused=False, data_available=True, details="calendar available")
    current = [unavailable]
    monkeypatch.setattr(main, "check_news_status", lambda _settings: current[0])
    pauses, resumes = [], []
    monkeypatch.setattr(main, "send_news_pause_alert", lambda details, _settings: pauses.append(details) or {"telegram": True})
    monkeypatch.setattr(main, "send_news_resume_alert", lambda *_args: resumes.append("resume") or {"telegram": True})
    monkeypatch.setattr(main, "_is_paused_for_news", False)
    monkeypatch.setattr(main, "_scan_deferred_by_news", False)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda _n: main.monitor_news_state(settings), range(8)))
    assert len(pauses) == 1

    # Simulate process restart while the persistent news pause is still active.
    monkeypatch.setattr(main, "_is_paused_for_news", False)
    monkeypatch.setattr(main, "_scan_deferred_by_news", False)
    main._load_news_pause_state()
    assert main.monitor_news_state(settings) is True
    assert len(pauses) == 1

    current[0] = healthy
    assert main.monitor_news_state(settings) is False
    assert main.monitor_news_state(settings) is False
    assert len(resumes) == 1


def test_news_pause_message_does_not_claim_a_real_event_or_post_to_channel(monkeypatch):
    from config import Settings
    from src import notifier

    settings = Settings(
        _env_file=None,
        telegram_bot_token="1:x",
        telegram_allowed_user_id="111",
        telegram_chat_id="-100group",
        telegram_channel_id="-100channel",
    )
    messages = []
    monkeypatch.setattr(notifier, "send_admin_message", lambda message, _settings: messages.append(message) or True)
    monkeypatch.setattr(notifier, "dispatch_message", lambda *_a, **_k: pytest.fail("pause notice must not broadcast"))

    result = notifier.send_news_pause_alert(
        "تعذر التحقق من تقويم الأخبار الاقتصادي؛ تم إيقاف التوصيات.", settings
    )

    assert result["telegram"] is True
    assert len(messages) == 1
    assert "تعذر التحقق من تقويم الأخبار" in messages[0]
    assert "لا يعني أن خبراً محدداً قد رُصد" in messages[0]
    assert "رصد أخبار اقتصادية قوية" not in messages[0]


def _alert(bar_time, timeframe="15m", strategy=None):
    return TradingViewAlert(
        ticker="EURUSD", timeframe=timeframe, signal="BUY", price=1.085,
        bar_time=bar_time, strategy=strategy,
        secret="test-secret-which-is-longer-than-32-chars",
    )


def test_tradingview_bar_time_accepts_fresh_epoch_and_rejects_missing_stale_or_future():
    now = datetime.now(timezone.utc)
    fresh = int(now.timestamp() * 1000)
    tradingview_bridge._validate_alert_time(_alert(str(fresh)), now=now)
    with pytest.raises(HTTPException) as missing:
        tradingview_bridge._validate_alert_time(_alert(None), now=now)
    assert missing.value.status_code == 422
    with pytest.raises(HTTPException) as stale:
        tradingview_bridge._validate_alert_time(_alert(str(int((now - timedelta(hours=1)).timestamp() * 1000))), now=now)
    assert stale.value.status_code == 422
    with pytest.raises(HTTPException) as future:
        tradingview_bridge._validate_alert_time(_alert(str(int((now + timedelta(minutes=5)).timestamp() * 1000))), now=now)
    assert future.value.status_code == 422


def test_tradingview_normalizes_supported_timeframes_and_rejects_unknown():
    assert _alert("1710000000000", timeframe="60").timeframe == "1h"
    with pytest.raises(ValueError):
        _alert("1710000000000", timeframe="5")


def test_strategy_label_is_honest_and_unknown_label_rejected():
    assert tradingview_bridge._strategy_for_alert(None) is StrategyType.EXTERNAL_ALERT
    assert tradingview_bridge._strategy_for_alert("pine_bridge") is StrategyType.EXTERNAL_ALERT
    assert tradingview_bridge._strategy_for_alert("trend_pullback") is StrategyType.TREND_PULLBACK
    with pytest.raises(HTTPException) as exc:
        tradingview_bridge._strategy_for_alert("mystery_alpha")
    assert exc.value.status_code == 422


def test_tradingview_requires_internal_direction_and_price_corroboration(monkeypatch):
    settings = SimpleNamespace(min_signal_confidence=0.7, min_risk_reward=2.0)
    frame = pd.DataFrame(
        {"Open": [1.085], "High": [1.086], "Low": [1.084], "Close": [1.085], "Volume": [1]},
        index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")]),
    )
    result = SimpleNamespace(metrics=SimpleNamespace(atr=0.001), symbol="EURUSD", timeframe="15m")
    signal = SimpleNamespace(direction=SimpleNamespace(value="BUY"))
    monkeypatch.setattr(tradingview_bridge, "get_settings", lambda: settings)
    monkeypatch.setattr(tradingview_bridge, "scan_regime_with_data", lambda *args, **kwargs: (result, frame))
    monkeypatch.setattr(tradingview_bridge, "generate_signal", lambda *_args, **_kwargs: signal)

    accepted = tradingview_bridge._corroborate_alert(_alert("1710000000000"))
    assert accepted[0] is signal
    assert accepted[2] is frame

    monkeypatch.setattr(
        tradingview_bridge,
        "generate_signal",
        lambda *_args, **_kwargs: SimpleNamespace(direction=SimpleNamespace(value="SELL")),
    )
    with pytest.raises(tradingview_bridge.AlertNotCorroborated, match="same-direction"):
        tradingview_bridge._corroborate_alert(_alert("1710000000000"))

    monkeypatch.setattr(tradingview_bridge, "generate_signal", lambda *_args, **_kwargs: signal)
    with pytest.raises(tradingview_bridge.AlertNotCorroborated, match="price does not match"):
        tradingview_bridge._corroborate_alert(_alert("1710000000000").model_copy(update={"price": 1.2}))
