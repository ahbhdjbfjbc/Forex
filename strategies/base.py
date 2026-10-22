"""Shared infrastructure for pluggable strategy implementations.

Each concrete strategy (`strategies/trend_pullback.py`,
`strategies/mean_reversion.py`, `strategies/breakout.py`) is a small class
implementing `Strategy.generate()`, plus a frozen dataclass of its tunable
parameters. Parameters are loaded from `data/strategy_params.json` via
`src.strategy_params` — this is what `strategy_evolver.py` mutates to test
variants, instead of editing strategy code directly.

`finalize_signal()` is the one piece of logic every strategy shares (R:R
targets, pip/unit-based risk sizing) — factored out here so it isn't
duplicated three times.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from config import Settings
from src import risk
from src.models import (
    MarketRegime,
    RegimeResult,
    RiskParameters,
    StrategyType,
    TradeDirection,
    TradeSignal,
)
from src.symbol_resolver import instrument_type

RR_TP2_BONUS = 1.0


class Strategy(ABC):
    """Common interface every strategy class implements."""

    name: str

    @abstractmethod
    def generate(
        self, regime_result: RegimeResult, df: pd.DataFrame, settings: Settings
    ) -> TradeSignal | None: ...


def finalize_signal(
    symbol: str,
    timeframe: str,
    direction: TradeDirection,
    strategy: StrategyType,
    regime: MarketRegime,
    entry_price: float,
    stop_loss_price: float,
    settings: Settings,
    rationale: str,
) -> TradeSignal:
    tp1_price, tp2_price = risk.compute_rr_targets(
        entry_price,
        stop_loss_price,
        direction,
        rr_tp1=settings.min_risk_reward,
        rr_tp2=settings.min_risk_reward + RR_TP2_BONUS,
    )

    if instrument_type(symbol) == "FX":
        # Standard pip/lot math applies.
        risk_params = risk.build_risk_parameters(
            symbol, settings.account_size_usd, settings.risk_percent_per_trade, entry_price, stop_loss_price
        )
        sl_dist = risk_params.stop_loss_pips
        tp1_dist = round(risk.price_to_pips(symbol, tp1_price - entry_price), 1)
        tp2_dist = round(risk.price_to_pips(symbol, tp2_price - entry_price), 1)
    else:
        # Metals/crypto don't have a standard "pip"/100k-unit-lot convention
        # (see src/risk.py). `stop_loss_pips`/`take_profit_*_pips` and
        # `risk.lot_size` are reused here to carry raw price distance and
        # raw unit count instead — callers must branch on
        # `symbol_resolver.instrument_type` to label them correctly for
        # display (src/telegram_bot.py does this).
        unit_size = risk.calculate_unit_size(
            settings.account_size_usd, settings.risk_percent_per_trade, entry_price, stop_loss_price
        )
        risk_params = RiskParameters(
            account_size_usd=settings.account_size_usd,
            risk_percent=settings.risk_percent_per_trade,
            stop_loss_pips=round(abs(entry_price - stop_loss_price), 4),
            pip_value_per_lot=0.0,
            lot_size=unit_size,
            risk_amount_usd=round(settings.account_size_usd * (settings.risk_percent_per_trade / 100), 2),
        )
        sl_dist = risk_params.stop_loss_pips
        tp1_dist = round(abs(tp1_price - entry_price), 4)
        tp2_dist = round(abs(tp2_price - entry_price), 4)

    return TradeSignal(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        strategy=strategy,
        regime=regime,
        entry_price=round(entry_price, 5),
        stop_loss_price=round(stop_loss_price, 5),
        stop_loss_pips=sl_dist,
        take_profit_1_price=round(tp1_price, 5),
        take_profit_1_pips=tp1_dist,
        take_profit_1_rr=settings.min_risk_reward,
        take_profit_2_price=round(tp2_price, 5),
        take_profit_2_pips=tp2_dist,
        take_profit_2_rr=settings.min_risk_reward + RR_TP2_BONUS,
        risk=risk_params,
        rationale=rationale,
    )
