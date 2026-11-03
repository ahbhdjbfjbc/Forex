"""Offline tests for persistent recommendation de-duplication and outcomes."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models import (
    MarketRegime,
    RiskParameters,
    StrategyType,
    TradeDirection,
    TradeSignal,
)
from src.signal_lifecycle import SignalLifecycleStore


def make_signal(
    *, symbol: str = "EURUSD=X", timeframe: str = "60",
    direction: TradeDirection = TradeDirection.BUY, entry: float = 1.1000,
) -> TradeSignal:
    if direction == TradeDirection.BUY:
        stop, target1, target2 = entry - 0.0100, entry + 0.0200, entry + 0.0300
    else:
        stop, target1, target2 = entry + 0.0100, entry - 0.0200, entry - 0.0300
    return TradeSignal(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        strategy=StrategyType.TREND_PULLBACK,
        regime=(
            MarketRegime.STRONG_TREND_BULLISH
            if direction == TradeDirection.BUY
            else MarketRegime.STRONG_TREND_BEARISH
        ),
        entry_price=entry,
        stop_loss_price=stop,
        stop_loss_pips=100,
        take_profit_1_price=target1,
        take_profit_1_pips=200,
        take_profit_1_rr=2,
        take_profit_2_price=target2,
        take_profit_2_pips=300,
        take_profit_2_rr=3,
        risk=RiskParameters(10_000, 1, 100, 10, 0.1, 100),
        rationale="test-only signal",
        timestamp=datetime.now(timezone.utc),
    )


def test_same_active_market_is_deduplicated_across_price_changes_and_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = SignalLifecycleStore(path)
    first = store.register_signal(make_signal(), source="test")
    assert first.accepted is True

    # TradingView may report a new close each minute; entry/stop/targets change,
    # but the same normalized pair/timeframe cannot create another open alert.
    second = store.register_signal(make_signal(entry=1.1005), source="tradingview")
    assert second.duplicate is True
    assert store.active_symbols() == ["EURUSD=X"]

    restarted = SignalLifecycleStore(path)
    assert restarted.register_signal(make_signal(entry=1.1010)).duplicate is True


def test_first_target_is_recorded_and_second_target_closes_once(tmp_path):
    store = SignalLifecycleStore(tmp_path / "state.sqlite3")
    registered = store.register_signal(make_signal(), entry_bar_id="bar-1")
    assert registered.accepted

    # Ignore the entry candle. A later candle reaching TP1 records progress but
    # keeps the recommendation active until TP2 or the stop is touched.
    assert store.observe_bar(
        "EURUSD", "1h", high=1.1205, low=1.0950, close=1.1190, bar_id="bar-1"
    ) is None
    assert store.observe_bar(
        "EURUSD", "1h", high=1.1210, low=1.0950, close=1.1200, bar_id="bar-2"
    ) is None

    reopened = SignalLifecycleStore(tmp_path / "state.sqlite3")
    outcome = reopened.observe_bar(
        "EURUSD=X", "60", high=1.1310, low=1.1150, close=1.1300, bar_id="bar-3"
    )
    assert outcome is not None
    assert outcome.outcome == "TAKE_PROFIT_2"
    assert outcome.tp1_reached is True
    assert outcome.r_multiple == 3.0
    assert reopened.observe_bar(
        "EURUSD", "1h", high=1.1400, low=1.1100, close=1.1350, bar_id="bar-4"
    ) is None
    assert len(reopened.pending_outcomes()) == 1

    reopened.mark_outcome_sent(outcome.signal_id)
    assert reopened.pending_outcomes() == []


def test_observe_frame_processes_bars_after_entry_in_order(tmp_path):
    import pandas as pd

    entry_bar = pd.Timestamp("2026-10-30T10:00:00Z")
    frame = pd.DataFrame(
        {
            "High": [1.1050, 1.1210, 1.1310],
            "Low": [1.0950, 1.0950, 1.1150],
            "Close": [1.1000, 1.1190, 1.1300],
        },
        index=[entry_bar, entry_bar + pd.Timedelta(hours=1), entry_bar + pd.Timedelta(hours=2)],
    )
    store = SignalLifecycleStore(tmp_path / "state.sqlite3")
    store.register_signal(make_signal(), entry_bar_id=str(entry_bar))

    outcome = store.observe_frame("EURUSD=X", "60", frame)

    assert outcome is not None
    assert outcome.outcome == "TAKE_PROFIT_2"
    assert outcome.tp1_reached is True


def test_sell_stop_loss_closes_with_negative_one_r(tmp_path):
    store = SignalLifecycleStore(tmp_path / "state.sqlite3")
    store.register_signal(make_signal(direction=TradeDirection.SELL))

    outcome = store.observe_bar(
        "EURUSD", "1h", high=1.1110, low=1.0950, close=1.1080, bar_id="later-bar"
    )

    assert outcome is not None
    assert outcome.outcome == "STOP_LOSS"
    assert outcome.exit_price == pytest.approx(1.1100)
    assert outcome.r_multiple == -1.0


def test_same_bar_stop_and_target_is_left_ambiguous_without_invented_exit(tmp_path):
    from src.notifier import format_signal_outcome

    store = SignalLifecycleStore(tmp_path / "state.sqlite3")
    store.register_signal(make_signal())

    outcome = store.observe_bar(
        "EURUSD", "1h", high=1.1310, low=1.0890, close=1.1050, bar_id="ambiguous-bar"
    )

    assert outcome is not None
    assert outcome.outcome == "AMBIGUOUS"
    assert outcome.exit_price is None
    assert outcome.r_multiple is None
    assert outcome.points is None
    message = format_signal_outcome(outcome)
    assert "نتيجة ملتبسة" in message
    assert "غير محسوبة" in message
    assert "غير قابل للتحديد" in message


def test_direction_reversal_closes_old_signal_and_opens_new_one(tmp_path):
    store = SignalLifecycleStore(tmp_path / "state.sqlite3")
    store.register_signal(make_signal())

    reversal = store.register_signal(
        make_signal(direction=TradeDirection.SELL, entry=1.1050), source="tradingview"
    )

    assert reversal.accepted is True
    assert reversal.closed_outcome is not None
    assert reversal.closed_outcome.outcome == "REVERSED"
    assert reversal.closed_outcome.exit_price == pytest.approx(1.1050)


def test_setup_reset_during_open_trade_allows_new_signal_after_trade_closes(tmp_path):
    store = SignalLifecycleStore(tmp_path / "state.sqlite3")
    original = store.register_signal(make_signal())
    store.reset_block("EURUSD", "1h")

    assert store.register_signal(make_signal(entry=1.1005)).duplicate is True
    outcome = store.observe_price("EURUSD", "1h", 1.1310)
    assert outcome is not None
    assert outcome.outcome == "TAKE_PROFIT_2"

    next_signal = store.register_signal(make_signal(entry=1.1400))
    assert next_signal.accepted is True
    assert next_signal.signal_id != original.signal_id


def test_outcome_records_candle_close_time_duration_and_fx_pips(tmp_path):
    from dataclasses import replace
    import pandas as pd

    from src.notifier import format_signal_outcome

    entry_time = pd.Timestamp("2026-10-30T10:00:00Z")
    close_time = entry_time + pd.Timedelta(minutes=90)
    signal = replace(make_signal(), timestamp=entry_time.to_pydatetime())
    store = SignalLifecycleStore(tmp_path / "state.sqlite3")
    store.register_signal(signal, source="test-source", entry_bar_id=entry_time)

    outcome = store.observe_bar(
        "EURUSD=X", "60", high=1.1310, low=1.1150, close=1.1300, bar_id=close_time
    )

    assert outcome is not None
    assert outcome.points == 300.0
    assert outcome.points_unit == "pips"
    assert outcome.duration_seconds == 90 * 60
    assert outcome.source == "test-source"
    assert outcome.stop_loss_price == signal.stop_loss_price
    assert outcome.target_price == signal.take_profit_2_price
    assert outcome.closed_at.startswith("2026-10-30T11:30:00")
    text = format_signal_outcome(outcome)
    assert "300 pips" in text
    assert "ساعة" in text
    assert "Bid/Ask" in text
    assert "وقف الخسارة" in text and "الهدف" in text
