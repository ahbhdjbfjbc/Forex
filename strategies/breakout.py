"""Volatility Breakout Strategy — for HIGH_VOLATILITY_EXPANSION regimes.

Entry trigger: close breaks the *prior* bar's Donchian channel with RVOL
expansion (confirms real participation, not just a wick). Stop loss is the
Keltner midline, falling back to an ATR buffer if that isn't on the correct
side of price.
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
class BreakoutParams:
    donchian_length: int = 20
    keltner_ema_length: int = 20
    keltner_atr_length: int = 10
    atr_length: int = 14
    atr_sl_multiplier: float = 1.5
    breakout_rvol_min: float = 1.2


class BreakoutStrategy(Strategy):
    name = "breakout"

    def __init__(self, params: BreakoutParams | None = None):
        self.params = params or BreakoutParams()

    def generate(
        self, regime_result: RegimeResult, df: pd.DataFrame, settings: Settings
    ) -> TradeSignal | None:
        p = self.params
        close = float(df["Close"].iloc[-1])
        donchian = ind.donchian_channel(df, p.donchian_length)
        keltner = ind.keltner_channel(df, p.keltner_ema_length, p.keltner_atr_length)
        # Compare against the *prior* bar's channel so the breakout is measured
        # against a level the current bar could actually have broken through.
        prior_upper = float(donchian["UPPER"].iloc[-2])
        prior_lower = float(donchian["LOWER"].iloc[-2])
        rvol = regime_result.metrics.rvol

        if close > prior_upper and rvol >= p.breakout_rvol_min:
            direction = TradeDirection.BUY
            stop_loss = float(keltner["MID"].iloc[-1])
            rationale = f"Volatility breakout LONG: close {close:.5f} > Donchian upper {prior_upper:.5f}, RVOL={rvol:.2f}"
        elif close < prior_lower and rvol >= p.breakout_rvol_min:
            direction = TradeDirection.SELL
            stop_loss = float(keltner["MID"].iloc[-1])
            rationale = f"Volatility breakout SHORT: close {close:.5f} < Donchian lower {prior_lower:.5f}, RVOL={rvol:.2f}"
        else:
            logger.info(
                "%s/%s: high-volatility regime but no Donchian breakout trigger (close=%.5f, rvol=%.2f)",
                regime_result.symbol, regime_result.timeframe, close, rvol,
            )
            return None

        if (direction == TradeDirection.BUY and stop_loss >= close) or (
            direction == TradeDirection.SELL and stop_loss <= close
        ):
            atr_val = float(ind.atr(df, p.atr_length).iloc[-1])
            stop_loss = (
                close - atr_val * p.atr_sl_multiplier
                if direction == TradeDirection.BUY
                else close + atr_val * p.atr_sl_multiplier
            )

        return finalize_signal(
            regime_result.symbol, regime_result.timeframe, direction, StrategyType.VOLATILITY_BREAKOUT,
            regime_result.regime, close, stop_loss, settings, rationale,
        )
