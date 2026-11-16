"""Regression tests for edge cases found while auditing live signal paths."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from config import Settings
from src import data_provider, publish_gate, risk, tradingview_bridge
from src.data_provider import LiveQuote
from src.models import TradingViewAlert
from src.signal_lifecycle import OutcomeHistoryGap, SignalLifecycleStore
from tests.test_signal_lifecycle import make_signal


def test_missing_or_nonfinite_volume_is_rejected_before_strategy_use():
    frame = pd.DataFrame(
        {
            "Open": [1.0, 1.0], "High": [1.1, 1.1], "Low": [0.9, 0.9],
            "Close": [1.05, 1.05], "Volume": [100.0, float("nan")],
        },
        index=pd.date_range("2026-11-16", periods=2, freq="h", tz="UTC"),
    )
    with pytest.raises(data_provider.DataFetchError, match="non-finite"):
        data_provider._validate(frame, data_provider.OHLCVRequest("EURUSD", "1h", min_bars=1))


def test_rvol_does_not_drop_nan_and_use_an_older_bar_as_latest():
    from src.session_utils import compute_rvol

    volumes = pd.Series([100.0] * 20 + [1000.0, float("nan")])
    assert compute_rvol(volumes) == 1.0


def test_oanda_quote_rejects_naive_timestamp(monkeypatch):
    url = "https://api-fxtrade.oanda.com/v3/accounts/test/pricing"
    settings = SimpleNamespace(
        oanda_api_token="test-token", oanda_account_id="test", oanda_environment="live"
    )
    payload = {"prices": [{
        "instrument": "EUR_USD", "bids": [{"price": "1.1"}],
        "asks": [{"price": "1.1002"}], "status": "tradeable",
        "time": "2026-11-16T19:00:00",
    }]}
    monkeypatch.setattr(
        data_provider.httpx, "get",
        lambda *_a, **_k: __import__("httpx").Response(
            200, request=__import__("httpx").Request("GET", url), json=payload
        ),
    )
    with pytest.raises(data_provider.DataFetchError, match="no timezone"):
        data_provider.get_oanda_live_quote("EURUSD", settings, use_cache=False)


def test_tradingview_rejects_timezone_less_iso_bar_time():
    alert = TradingViewAlert(
        ticker="EURUSD", timeframe="15m", signal="BUY", price=1.1,
        bar_time="2026-11-16T19:00:00",
    )
    with pytest.raises(Exception) as exc:
        tradingview_bridge._validate_alert_time(alert)
    assert getattr(exc.value, "status_code", None) == 422


def test_tradingview_quote_drift_is_measured_from_original_pine_risk(monkeypatch):
    from src.data_provider import LiveQuote

    alert = TradingViewAlert(
        ticker="EURUSD", timeframe="15m", signal="BUY", price=1.085,
        stop_loss=1.083, take_profit=1.089, take_profit_2=1.091,
    )
    quote = LiveQuote(
        "EUR_USD", 1.0855, 1.0856, datetime.now(timezone.utc), "tradeable"
    )
    monkeypatch.setattr("src.data_provider.get_oanda_live_quote", lambda *_a, **_k: quote)
    with pytest.raises(ValueError, match="original TradingView entry"):
        tradingview_bridge._apply_tradingview_levels(
            make_signal(entry=1.085), alert, Settings(_env_file=None)
        )


def test_live_cross_pair_lot_size_uses_independent_oanda_usd_conversion(monkeypatch):
    now = datetime.now(timezone.utc)
    conversion = LiveQuote("GBP_USD", 0.82, 0.8202, now, "tradeable")
    monkeypatch.setattr(
        data_provider, "get_oanda_live_quote", lambda symbol, *_a, **_k: conversion
    )
    quote = LiveQuote("EUR_GBP", 0.8600, 0.8602, now, "tradeable")
    settings = Settings(_env_file=None, data_provider="oanda", account_size_usd=10_000)

    repriced = risk.reprice_signal_to_quote(
        make_signal(symbol="EURGBP", entry=0.86), quote, settings
    )

    # GBP/USD bid converts a 10 GBP pip into 8.2 USD; a $100 budget and
    # 100-pip stop therefore round down to 0.12 standard lots.
    assert repriced.risk.pip_value_per_lot == pytest.approx(8.2)
    assert repriced.risk.lot_size == 0.12


def test_live_cross_pair_repricing_fails_closed_without_currency_conversion(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise data_provider.DataFetchError("conversion unavailable")

    monkeypatch.setattr(data_provider, "get_oanda_live_quote", unavailable)
    quote = LiveQuote(
        "EUR_GBP", 0.8600, 0.8602, datetime.now(timezone.utc), "tradeable"
    )

    with pytest.raises(ValueError, match="fresh OANDA conversion unavailable"):
        risk.reprice_signal_to_quote(
            make_signal(symbol="EURGBP", entry=0.86),
            quote,
            Settings(_env_file=None, data_provider="oanda"),
        )


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-03-08T20:59:00Z", False),  # Sunday before 17:00 EDT
        ("2026-03-08T21:00:00Z", True),   # Sunday 17:00 EDT
        ("2026-03-06T22:00:00Z", False),  # Friday 17:00 EST
        ("2026-03-09T20:59:00Z", True),
    ],
)
def test_fx_market_hours_follow_new_york_dst(stamp, expected):
    assert publish_gate.fx_market_open(datetime.fromisoformat(stamp.replace("Z", "+00:00"))) is expected


def test_outcome_monitor_raises_instead_of_inventing_result_when_history_is_missing(tmp_path):
    entry = pd.Timestamp("2026-01-01T00:00:00Z")
    store = SignalLifecycleStore(tmp_path / "history-gap.sqlite3")
    store.register_signal(make_signal(), entry_bar_id=entry)
    store.observe_bar(
        "EURUSD", "1h", high=1.105, low=1.095, close=1.1,
        bar_id=entry + pd.Timedelta(hours=1), mark_evaluated=True,
    )
    missing_window = pd.DataFrame(
        {"High": [1.131], "Low": [1.115], "Close": [1.13]},
        index=[entry + pd.Timedelta(days=30)],
    )
    with pytest.raises(OutcomeHistoryGap):
        store.observe_frame("EURUSD", "1h", missing_window)
    assert store.performance_summary()["active"] == 1
    assert store.pending_outcomes() == []


def test_pending_signal_is_not_monitored_until_telegram_message_receipt_is_saved(tmp_path):
    store = SignalLifecycleStore(tmp_path / "delivery.sqlite3")
    registration = store.register_signal(
        make_signal(), entry_bar_id="bar0", require_delivery=True
    )
    assert registration.accepted
    assert store.active_markets() == []
    assert store.set_telegram_delivery(registration.signal_id, 1234, "-100original")
    assert store.active_markets() == [("EURUSD=X", "1h")]
    assert store.telegram_delivery(registration.signal_id) == (1234, "-100original")


def test_result_reply_uses_saved_original_channel_not_current_configuration(monkeypatch, tmp_path):
    from src import notifier

    store = SignalLifecycleStore(tmp_path / "result-channel.sqlite3")
    monkeypatch.setattr(notifier, "signal_lifecycle", store)
    registration = store.register_signal(make_signal(), entry_bar_id="bar0")
    assert store.set_telegram_delivery(registration.signal_id, 9876, "-100original")
    outcome = store.observe_bar(
        "EURUSD", "1h", high=1.131, low=1.115, close=1.13, bar_id="bar1"
    )
    assert outcome is not None
    sent = []
    monkeypatch.setattr(
        notifier, "send_telegram",
        lambda text, _settings, **kwargs: sent.append((text, kwargs)) or True,
    )
    monkeypatch.setattr(
        notifier, "dispatch_message",
        lambda *_a, **_k: pytest.fail("outcome must not be duplicated to other channels"),
    )
    settings = Settings(_env_file=None, telegram_channel_id="-100new-channel")
    result = notifier.send_signal_outcome(outcome, settings)
    assert result["telegram"] is True
    assert sent[0][1]["target_chat_id"] == "-100original"
    assert sent[0][1]["reply_to_message_id"] == 9876
    assert store.pending_outcomes() == []


def test_paper_executor_attaches_final_tp2_not_intermediate_tp1(monkeypatch):
    from src import paper_executor

    signal = make_signal()
    captured = []
    monkeypatch.setattr(
        paper_executor.oanda_client, "place_market_order",
        lambda *args, **_kwargs: captured.append(args) or SimpleNamespace(
            status="FILLED", trade_id="demo-id"
        ),
    )
    settings = Settings(
        _env_file=None, enable_paper_trading=True,
        paper_trading_env="practice", paper_account_id="practice", paper_api_token="token",
    )
    assert paper_executor.execute_paper_trade(signal, settings)
    assert captured[0][4] == signal.take_profit_2_price


def test_pine_exposes_only_the_json_alert_function_workflow():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "pine_script" / "tv_indicator.pine").read_text(
        encoding="utf-8"
    )
    assert "alertcondition(" not in source
    assert "Any alert() function call" in source
    assert "supportedWebhookTimeframe" in source
    assert '"stop_loss":' in source and '"take_profit_2":' in source
