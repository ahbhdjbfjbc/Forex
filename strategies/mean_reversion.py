"""Mean Reversion / Support-Resistance Fade Strategy — for RANGING_CONSOLIDATION.

Entry trigger: price at/beyond the outer Bollinger Band with RSI confirming
overbought (fade short) or oversold (fade long). Stop loss is a small ATR
buffer beyond the band.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from config import Settings
from src import indicators as ind
from src.models import RegimeResult, StrategyType, TradeDirection, TradeSignal
from strategies.base import Strategy, finalize_signal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MeanReversionParams:
    bb_length: int = 20
    rsi_length: int = 14
    atr_length: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    sl_atr_buffer: float = 0.5


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    def __init__(self, params: MeanReversionParams | None = None):
        self.params = params or MeanReversionParams()

    def generate(
        self, regime_result: RegimeResult, df: pd.DataFrame, settings: Settings
    ) -> TradeSignal | None:
        p = self.params
        close = float(df["Close"].iloc[-1])
        bb = ind.bollinger_bands(df["Close"], p.bb_length)
        upper, lower = float(bb["UPPER"].iloc[-1]), float(bb["LOWER"].iloc[-1])
        rsi_now = float(ind.rsi(df["Close"], p.rsi_length).iloc[-1])
        atr_val = float(ind.atr(df, p.atr_length).iloc[-1])

        if close >= upper and rsi_now >= p.rsi_overbought:
            direction = TradeDirection.SELL
            stop_loss = close + atr_val * p.sl_atr_buffer
            rationale = f"Mean-reversion fade SHORT: close at/above upper BB ({upper:.5f}), RSI={rsi_now:.1f} overbought"
        elif close <= lower and rsi_now <= p.rsi_oversold:
            direction = TradeDirection.BUY
            stop_loss = close - atr_val * p.sl_atr_buffer
            rationale = f"Mean-reversion fade LONG: close at/below lower BB ({lower:.5f}), RSI={rsi_now:.1f} oversold"
        else:
            logger.info(
                "%s/%s: ranging regime but no BB/RSI fade trigger (close=%.5f, upper=%.5f, lower=%.5f, rsi=%.1f)",
                regime_result.symbol, regime_result.timeframe, close, upper, lower, rsi_now,
            )
            return None

        return finalize_signal(
            regime_result.symbol, regime_result.timeframe, direction, StrategyType.MEAN_REVERSION_FADE,
            regime_result.regime, close, stop_loss, settings, rationale,
        )
