"""Scanner 2 — Strategy Selector & Trade Signal Generator.

Thin dispatcher: maps a `RegimeResult`'s regime to its matched strategy
class (`strategies/`), loads that strategy's current tunable parameters
(`src.strategy_params`, backed by `data/strategy_params.json`), and
delegates to `Strategy.generate()`. No indicator/strategy math lives here
directly — see the individual `strategies/*.py` modules.

    STRONG_TREND_BULLISH/BEARISH   -> TrendPullbackStrategy
    RANGING_CONSOLIDATION          -> MeanReversionStrategy
    HIGH_VOLATILITY_EXPANSION      -> BreakoutStrategy
    LOW_LIQUIDITY_CHOP             -> NO_TRADE (filtered out, returns None)

`param_overrides` (keyed by strategy name) lets `backtester.py` /
`strategy_evolver.py` test a candidate parameter variant in-memory, without
touching `data/strategy_params.json`.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config import Settings, get_settings
from src.models import MarketRegime, RegimeResult, TradeSignal
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
        return strategy.generate(regime_result, df, settings)
    except Exception:
        logger.exception(
            "signal generation failed for %s/%s (%s)",
            regime_result.symbol, regime_result.timeframe, regime_result.regime,
        )
        return None
