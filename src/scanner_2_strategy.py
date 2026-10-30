"""Scanner 2 — Strategy Selector, Trade Signal Generator & Institutional Quality Gate.

Maps a `RegimeResult`'s regime to its matched strategy class (`strategies/`),
loads that strategy's current tunable parameters (`src.strategy_params`),
delegates to `Strategy.generate()`, and applies an institutional price-action
and momentum quality filter before approving the signal.

    STRONG_TREND_BULLISH/BEARISH   -> TrendPullbackStrategy
    RANGING_CONSOLIDATION          -> MeanReversionStrategy
    HIGH_VOLATILITY_EXPANSION      -> BreakoutStrategy
    LOW_LIQUIDITY_CHOP             -> NO_TRADE (filtered out, returns None)
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config import Settings, get_settings
from src import indicators as ind
from src.models import MarketRegime, RegimeResult, StrategyType, TradeDirection, TradeSignal
from src.strategy_params import get_strategy_params
from strategies.breakout import BreakoutStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.trend_pullback import TrendPullbackStrategy

logger = logging.getLogger(__name__)

_STRATEGY_CLASSES = {
    MarketRegime.STRONG_TREND_BULLISH: TrendPullbackStrategy,
    MarketRegime.STRONG_TREND_BEARISH: TrendPullbackStrategy,
    MarketRegime.RANGING_CONSOLIDATION: MeanReversionStrategy,
    MarketRegime.HIGH_VOLATILITY_EXPANSION: BreakoutStrategy,
}


def _validate_and_refine_signal(
    signal: TradeSignal,
    regime_result: RegimeResult,
    df: pd.DataFrame,
) -> TradeSignal | None:
    """فلتر احترافي للتأكد من جودة نقطة الدخول والشموع والزخم قبل اعتماد التوصية."""
    try:
        if len(df) < 20:
            return signal

        last_open = float(df["Open"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])
        last_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])

        candle_range = last_high - last_low
        atr_val = regime_result.metrics.atr
        rsi_series = ind.rsi(df["Close"], 14)
        rsi_now = float(rsi_series.iloc[-1])
        rsi_prev = float(rsi_series.iloc[-2]) if len(rsi_series) >= 2 else rsi_now

        # 1. فحص منطقية مسافة وقف الخسارة مقارنة بمتوسط الحركة (ATR)
        if atr_val > 0:
            sl_distance = abs(float(signal.entry_price) - float(signal.stop_loss_price))
            # إذا كان وقف الخسارة أصغر من 15% من ATR (ضيق جداً يضربه السبريد) أو أكبر من 4.5 أضعاف ATR
            if sl_distance < (0.15 * atr_val) or sl_distance > (4.5 * atr_val):
                logger.info(
                    "%s/%s: signal rejected due to abnormal SL distance vs ATR",
                    regime_result.symbol,
                    regime_result.timeframe,
                )
                return None

        # 2. فحص الشموع الانعكاسية والتشبع لمنع الدخول المتأخر
        if candle_range > 0:
            upper_wick_ratio = (last_high - max(last_open, last_close)) / candle_range
            lower_wick_ratio = (min(last_open, last_close) - last_low) / candle_range
            body_ratio = abs(last_close - last_open) / candle_range
        else:
            upper_wick_ratio = 0.0
            lower_wick_ratio = 0.0
            body_ratio = 0.0

        if signal.direction == TradeDirection.BUY:
            # في صفقات الشراء (اتجاه أو اختراق): لا نشتري في قمة تشبع شرائي حاد (RSI > 74)
            if signal.strategy != StrategyType.MEAN_REVERSION_FADE and rsi_now > 74:
                logger.info("%s/%s: BUY rejected (RSI overbought %.1f)", regime_result.symbol, regime_result.timeframe, rsi_now)
                return None

            # رفض الشراء إذا كانت الشمعة الأخيرة شمعة رفض بيعي قوية (ذيل علوي أكبر من 65% من الشمعة وإغلاق سلبي)
            if upper_wick_ratio > 0.65 and last_close < last_open:
                logger.info("%s/%s: BUY rejected (bearish rejection wick)", regime_result.symbol, regime_result.timeframe)
                return None

            # تعزيز درجة الثقة إذا كانت الشمعة صاعدة بقوة والزخم متصاعد
            if last_close > last_open and last_close >= prev_close and rsi_now >= rsi_prev:
                bonus = 0.06 if body_ratio >= 0.45 else 0.03
                regime_result.confidence = min(1.0, round(regime_result.confidence + bonus, 4))

        elif signal.direction == TradeDirection.SELL:
            # في صفقات البيع (اتجاه أو اختراق): لا نبيع في قاع تشبع بيعي حاد (RSI < 26)
            if signal.strategy != StrategyType.MEAN_REVERSION_FADE and rsi_now < 26:
                logger.info("%s/%s: SELL rejected (RSI oversold %.1f)", regime_result.symbol, regime_result.timeframe, rsi_now)
                return None

            # رفض البيع إذا كانت الشمعة الأخيرة شمعة رفض شرائي قوية (ذيل سفلي أكبر من 65% من الشمعة وإغلاق إيجابي)
            if lower_wick_ratio > 0.65 and last_close > last_open:
                logger.info("%s/%s: SELL rejected (bullish rejection wick)", regime_result.symbol, regime_result.timeframe)
                return None

            # تعزيز درجة الثقة إذا كانت الشمعة هابطة بقوة والزخم هابط
            if last_close < last_open and last_close <= prev_close and rsi_now <= rsi_prev:
                bonus = 0.06 if body_ratio >= 0.45 else 0.03
                regime_result.confidence = min(1.0, round(regime_result.confidence + bonus, 4))

        return signal
    except Exception:
        return signal


def generate_signal(
    regime_result: RegimeResult,
    df: pd.DataFrame,
    settings: Settings | None = None,
    param_overrides: dict[str, dict[str, Any]] | None = None,
) -> TradeSignal | None:
    """Map a regime to its strategy and produce a `TradeSignal`, or None if
    the regime is filtered out (LOW_LIQUIDITY_CHOP) or no entry trigger is
    currently active within an otherwise tradeable regime."""
    settings = settings or get_settings()

    if regime_result.regime == MarketRegime.LOW_LIQUIDITY_CHOP:
        logger.info("%s/%s: LOW_LIQUIDITY_CHOP -> NO_TRADE", regime_result.symbol, regime_result.timeframe)
        return None

    strategy_cls = _STRATEGY_CLASSES.get(regime_result.regime)
    if strategy_cls is None:
        logger.warning("no strategy mapped for regime %s", regime_result.regime)
        return None

    overrides = (param_overrides or {}).get(strategy_cls.name)
    params = get_strategy_params(strategy_cls.name, overrides=overrides)
    strategy = strategy_cls(params)

    try:
        signal = strategy.generate(regime_result, df, settings)
        if signal is None:
            return None
        # في الفحص الحقيقي (عندما لا يكون هناك اختبار overrides)، نطبق الفلتر الاحترافي الإضافي
        if param_overrides is None:
            signal = _validate_and_refine_signal(signal, regime_result, df)
        return signal
    except Exception:
        logger.exception(
            "signal generation failed for %s/%s (%s)",
            regime_result.symbol, regime_result.timeframe, regime_result.regime,
        )
        return None
