"""Tests for the final publication gate shared by every publish path."""
from datetime import datetime, timezone

import pytest

from config import Settings
from src import news_filter, publish_gate
from src.models import MarketRegime, StrategyType, TradeDirection
from tests.test_signal_lifecycle import make_signal

pytestmark = pytest.mark.real_publish_gate

WEDNESDAY_NOON = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def _settings(**overrides):
    base = dict(_env_file=None, enable_news_filter=False, enable_mtf_filter=False, enable_dxy_filter=False)
    base.update(overrides)
    return Settings(**base)


def test_market_hours():
    assert publish_gate.fx_market_open(WEDNESDAY_NOON)
    assert not publish_gate.fx_market_open(datetime(2026, 11, 7, 12, tzinfo=timezone.utc))   # Saturday
    assert not publish_gate.fx_market_open(datetime(2026, 11, 6, 21, 30, tzinfo=timezone.utc))  # Fri late
    assert not publish_gate.fx_market_open(datetime(2026, 11, 8, 20, tzinfo=timezone.utc))   # Sun early
    assert publish_gate.fx_market_open(datetime(2026, 11, 8, 22, 30, tzinfo=timezone.utc))
    weekend = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    blocked = publish_gate.check_publish_allowed(make_signal(), _settings(), now=weekend)
    assert not blocked.allowed and "مغلق" in blocked.reason
    crypto = publish_gate.check_publish_allowed(make_signal(symbol="BTCUSD"), _settings(), now=weekend)
    assert crypto.allowed


def test_news_is_symbol_specific_and_fails_closed(monkeypatch):
    now = datetime.now(timezone.utc)
    events = [{"impact": "High", "country": "JPY", "title": "BoJ", "date": now.isoformat()}]
    monkeypatch.setattr(news_filter, "_fetch_calendar_events", lambda: events)
    settings = _settings(enable_news_filter=True, enable_market_hours_filter=False)
    assert publish_gate.check_publish_allowed(make_signal(), settings).allowed  # EURUSD unaffected
    blocked = publish_gate.check_publish_allowed(make_signal(symbol="USDJPY=X"), settings)
    assert not blocked.allowed and "JPY" in blocked.reason
    assert news_filter.currencies_for_symbol("XAUUSD") == {"USD"}

    monkeypatch.setattr(news_filter, "_fetch_calendar_events", lambda: None)
    assert not publish_gate.check_publish_allowed(make_signal(), settings).allowed


def test_mtf_opposite_strong_trend_blocks_and_missing_data_fails_closed(monkeypatch):
    from tests import mock_data as md
    from src.scanner_1_regime import _build_metrics
    from src.models import RegimeResult

    settings = _settings(enable_mtf_filter=True, enable_market_hours_filter=False)
    bearish = md.make_trend_pullback_bearish()
    result = RegimeResult("EURUSD", "4h", MarketRegime.STRONG_TREND_BEARISH, _build_metrics(bearish), 0.9)
    monkeypatch.setattr("src.scanner_1_regime.scan_regime_with_data", lambda *a, **k: (result, bearish))
    decision = publish_gate.check_publish_allowed(make_signal(timeframe="1h"), settings)
    assert not decision.allowed and "4h" in decision.reason
    sell = make_signal(timeframe="1h", direction=TradeDirection.SELL)
    assert publish_gate.check_publish_allowed(sell, settings).allowed

    monkeypatch.setattr("src.scanner_1_regime.scan_regime_with_data", lambda *a, **k: None)
    assert not publish_gate.check_publish_allowed(make_signal(timeframe="1h"), settings).allowed


def test_gold_dxy_filter(monkeypatch):
    settings = _settings(enable_dxy_filter=True, enable_market_hours_filter=False)
    gold_buy = make_signal(symbol="XAUUSD", entry=4000.0)
    monkeypatch.setattr(publish_gate, "_fetch_dxy_change_pct", lambda: 0.8)
    assert not publish_gate.check_publish_allowed(gold_buy, settings).allowed
    monkeypatch.setattr(publish_gate, "_fetch_dxy_change_pct", lambda: None)
    assert publish_gate.check_publish_allowed(gold_buy, settings).allowed  # optional context
    monkeypatch.setattr(publish_gate, "_fetch_dxy_change_pct", lambda: 0.8)
    assert publish_gate.check_publish_allowed(make_signal(), settings).allowed  # not gold


def test_send_alert_returns_blocked_reason(monkeypatch):
    import pandas as pd
    from src import notifier

    monkeypatch.setattr(publish_gate, "check_publish_allowed",
                        lambda *a, **k: publish_gate.GateDecision(False, "سبب اختبار"))
    called = []
    monkeypatch.setattr(notifier, "dispatch_signal", lambda *a, **k: called.append(1) or {"telegram": True})
    frame = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0]})
    result = notifier.send_alert(make_signal(), _settings(), market_frame=frame)
    assert result.blocked_reason == "سبب اختبار"
    assert not any(v for k, v in result.items() if k != "duplicate")
    assert called == []
