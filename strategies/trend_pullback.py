"""Trend-Following Pullback Strategy — for STRONG_TREND_BULLISH/BEARISH regimes.

Entry trigger: price touches EMA20 AND (RSI shows the pullback resolving
back in the trend's direction, OR price is inside the 38.2-61.8% Fibonacci
retracement zone of the last swing). Stop loss is the tighter of the last
swing point or an ATR-based buffer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from config import Settings
from src import indicators as ind
from src.models import MarketRegime, RegimeResult, StrategyType, TradeDirection, TradeSignal
from strategies.base import Strategy, finalize_signal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrendPullbackParams:
    rsi_length: int = 14
    atr_length: int = 14
    structure_lookback: int = 50
    ema_touch_tolerance_pct: float = 0.0015  # 0.15% of price counts as a "touch"
    atr_sl_multiplier: float = 1.5
    rsi_bullish_ceiling: float = 55.0
    rsi_bearish_floor: float = 45.0


class TrendPullbackStrategy(Strategy):
    name = "trend_pullback"

    def __init__(self, params: TrendPullbackParams | None = None):
        self.params = params or TrendPullbackParams()

    def generate(
        self, regime_result: RegimeResult, df: pd.DataFrame, settings: Settings
    ) -> TradeSignal | None:
        p = self.params
        is_bullish = regime_result.regime == MarketRegime.STRONG_TREND_BULLISH
        direction = TradeDirection.BUY if is_bullish else TradeDirection.SELL

        close = float(df["Close"].iloc[-1])
        ema20 = float(ind.ema(df["Close"], 20).iloc[-1])
        ema20_touch = float(df["Low"].iloc[-1]) <= ema20 <= float(df["High"].iloc[-1]) or (
            abs(close - ema20) / close <= p.ema_touch_tolerance_pct
        )

        rsi_series = ind.rsi(df["Close"], p.rsi_length)
        rsi_now, rsi_prev = float(rsi_series.iloc[-1]), float(rsi_series.iloc[-2])
        rsi_turning_up = rsi_now > rsi_prev
        rsi_pullback_confirmed = (
            (rsi_now < p.rsi_bullish_ceiling and rsi_turning_up)
            if is_bullish
            else (rsi_now > p.rsi_bearish_floor and not rsi_turning_up)
        )

        structure = ind.detect_structure(df, p.structure_lookback)
        fib_zone_hit = False
        if structure["swing_highs"] and structure["swing_lows"]:
            swing_high = max(pr for _, pr in structure["swing_highs"])
            swing_low = min(pr for _, pr in structure["swing_lows"])
            fibs = ind.fib_retracement_levels(swing_high, swing_low)
            zone_high, zone_low = fibs["0.382"], fibs["0.618"]
            fib_zone_hit = min(zone_low, zone_high) <= close <= max(zone_low, zone_high)

        if not (ema20_touch and (rsi_pullback_confirmed or fib_zone_hit)):
            logger.info(
                "%s/%s: trend regime but no pullback entry trigger yet (ema20_touch=%s, rsi_confirmed=%s, fib=%s)",
                regime_result.symbol, regime_result.timeframe, ema20_touch, rsi_pullback_confirmed, fib_zone_hit,
            )
            return None

        atr_val = float(ind.atr(df, p.atr_length).iloc[-1])
        swing_lows = structure["swing_lows"]
        swing_highs = structure["swing_highs"]

        if is_bullish:
            structural_sl = min(pr for _, pr in swing_lows) if swing_lows else close - atr_val * p.atr_sl_multiplier
            stop_loss = min(structural_sl, close - atr_val * p.atr_sl_multiplier)
        else:
            structural_sl = max(pr for _, pr in swing_highs) if swing_highs else close + atr_val * p.atr_sl_multiplier
            stop_loss = max(structural_sl, close + atr_val * p.atr_sl_multiplier)

        rationale = (
            f"Trend pullback ({'bullish' if is_bullish else 'bearish'}): EMA20 touch="
            f"{ema20_touch}, RSI pullback confirmed={rsi_pullback_confirmed}, fib 38.2-61.8 zone={fib_zone_hit}"
        )
        return finalize_signal(
            regime_result.symbol, regime_result.timeframe, direction, StrategyType.TREND_PULLBACK,
            regime_result.regime, close, stop_loss, settings, rationale,
        )
