"""Regression tests for gold-specific structure and the rule-based quality gate."""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from config import Settings
from src.gold_analysis import GoldMarketContext, analyze_gold_market, validate_gold_entry
from src.models import MarketRegime, RiskParameters, StrategyType, TradeDirection, TradeSignal
from src.signal_quality import assess_signal_quality


def test_gold_analysis_uses_prior_bars_and_reports_range_context():
    close = np.linspace(2000.0, 2020.0, 60)
    frame = pd.DataFrame(
        {
            "Open": close - 1.0,
            "High": close + 2.0,
            "Low": close - 2.0,
            "Close": close,
        },
        index=pd.date_range("2026-10-01", periods=60, freq="1h", tz="UTC"),
    )
    context = analyze_gold_market(frame, atr=5.0)
    assert context is not None
    assert context.close == close[-1]
    assert context.resistance == frame["High"].iloc[:-1].max()
    assert context.atr_percent > 0


def test_gold_setup_rejects_target_blocked_by_unbroken_resistance():
    context = GoldMarketContext(
        close=2005, atr=10, atr_percent=0.5, support=1980, resistance=2020,
        prior_range_high=2020, prior_range_low=1980,
        close_breaks_range_high=False, close_breaks_range_low=False,
        current_range_atr=0.8,
    )
    valid, reason = validate_gold_entry(context, "BUY", entry=2010, take_profit=2030)
    assert valid is False
    assert "مقاومة" in reason


def test_gold_setup_accepts_confirmed_close_breakout():
    context = GoldMarketContext(
        close=2025, atr=10, atr_percent=0.5, support=1980, resistance=2020,
        prior_range_high=2020, prior_range_low=1980,
        close_breaks_range_high=True, close_breaks_range_low=False,
        current_range_atr=1.1,
    )
    valid, _reason = validate_gold_entry(context, "BUY", entry=2010, take_profit=2030)
    assert valid is True


def _signal():
    return TradeSignal(
        symbol="EURUSD", timeframe="1h", direction=TradeDirection.BUY,
        strategy=StrategyType.TREND_PULLBACK, regime=MarketRegime.STRONG_TREND_BULLISH,
        entry_price=1.1, stop_loss_price=1.095, stop_loss_pips=50,
        take_profit_1_price=1.11, take_profit_1_pips=100, take_profit_1_rr=2.0,
        take_profit_2_price=1.115, take_profit_2_pips=150, take_profit_2_rr=3.0,
        risk=RiskParameters(10000, 1, 50, 10, 0.2, 100),
        rationale="test", timestamp=datetime.now(timezone.utc),
    )


def test_rule_based_quality_grade_is_not_win_probability_and_respects_threshold():
    frame = pd.DataFrame(
        {"Close": [1.09, 1.10]},
        index=pd.date_range("2026-10-01", periods=2, freq="1h", tz="UTC"),
    )
    settings = Settings(_env_file=None, min_signal_confidence=0.7, min_signal_quality_score=70)
    result = assess_signal_quality(
        _signal(), 0.9, MarketRegime.STRONG_TREND_BULLISH, atr=0.01, df=frame, settings=settings
    )
    assert result.eligible is True
    assert result.grade in {"A", "B"}
    assert result.score >= settings.min_signal_quality_score
    assert any("العائد/المخاطرة" in reason for reason in result.reasons)

    strict_settings = Settings(_env_file=None, min_signal_confidence=0.7, min_signal_quality_score=100)
    blocked = assess_signal_quality(
        _signal(), 0.9, MarketRegime.STRONG_TREND_BULLISH, atr=0.01, df=frame, settings=strict_settings
    )
    assert blocked.eligible is False
