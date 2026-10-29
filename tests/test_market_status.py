"""Regression tests for Arabic automatic-scan status messages."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main as app
from config import Settings
from src.market_status import MarketCycleSummary, format_market_status
from src.models import MarketRegime


def test_market_status_messages_are_enabled_by_default() -> None:
    """Deployments report status unless an operator explicitly turns it off."""
    assert Settings(_env_file=None).send_market_status is True


def _summary(**overrides) -> MarketCycleSummary:
    """Build a minimal scan-cycle summary for formatting tests."""
    values = {
        "symbols_count": 1,
        "timeframe_count": 1,
        "data_pairs": 1,
        "qualified_signals": 0,
        "delivered_signals": 0,
        "low_confidence": 0,
        "no_entry": 0,
        "failed_deliveries": 0,
        "confidence_threshold": 0.6,
        "regime_counts": {},
    }
    values.update(overrides)
    return MarketCycleSummary(**values)


def test_status_explains_no_entry_and_waits_in_arabic() -> None:
    """A missing entry trigger yields a clear wait message and regime reason."""
    text = format_market_status(
        _summary(
            no_entry=1,
            regime_counts={MarketRegime.LOW_LIQUIDITY_CHOP: 1},
        )
    )

    assert "لا توجد نقطة دخول مؤكدة" in text
    assert "انتظر تأكيداً جديداً" in text
    assert "تذبذب مع سيولة منخفضة: 1" in text
    assert "فلتر تقويم اقتصادي حي" in text


def test_status_reports_missing_data_without_claiming_market_is_bad() -> None:
    """Missing prices are reported as unavailable, not as a market judgment."""
    text = format_market_status(
        _summary(symbols_count=2, data_pairs=0)
    )

    assert "تعذر جلب بيانات 2 حالة" in text
    assert "لا يمكن تأكيد وضعها" in text
    assert "السوق سيئ" not in text


def test_scan_cycle_sends_one_status_when_no_signal(monkeypatch) -> None:
    """The scheduler sends one Arabic status when no setup is generated."""
    settings = Settings(_env_file=None, timeframes="1h", send_market_status=True)
    regime = SimpleNamespace(
        confidence=0.8,
        regime=MarketRegime.LOW_LIQUIDITY_CHOP,
        symbol="EURUSD",
        timeframe="1h",
    )
    sent: list[str] = []

    def capture_status(message: str, _settings: Settings) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(app.watchlist_store, "get_all", lambda: ["EURUSD"])
    monkeypatch.setattr(
        app, "scan_symbols_with_data", lambda *_args, **_kwargs: [(regime, object())]
    )
    monkeypatch.setattr(app, "generate_signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        app,
        "send_alert",
        lambda *_args, **_kwargs: pytest.fail("no trade alert expected"),
    )
    monkeypatch.setattr(app, "send_telegram", capture_status)

    app.run_scan_cycle(settings)

    assert len(sent) == 1
    assert "لا توجد توصية جديدة" in sent[0]
    assert "انتظر تأكيداً جديداً" in sent[0]


def test_scan_cycle_can_disable_status_without_changing_scan(monkeypatch) -> None:
    """Disabling status messages does not disable or alter the scan itself."""
    settings = Settings(_env_file=None, timeframes="1h", send_market_status=False)
    sent: list[str] = []

    monkeypatch.setattr(app.watchlist_store, "get_all", lambda: ["EURUSD"])
    monkeypatch.setattr(app, "scan_symbols_with_data", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        app,
        "send_telegram",
        lambda message, _settings: sent.append(message) or True,
    )

    app.run_scan_cycle(settings)

    assert not sent
