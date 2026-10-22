"""Walk-forward backtesting engine.

Deliberately reuses the *exact* same code that runs live —
`src.scanner_1_regime.classify_regime`/`_build_metrics` and
`src.scanner_2_strategy.generate_signal` — rather than a separate
vectorized/DSL reimplementation of the strategies. A backtest that runs
different logic than the live bot is worse than no backtest at all: it
creates false confidence, since a passing result says nothing about what
the live bot would actually have done. The only way to guarantee they match
is to literally call the same functions.

No lookahead: at bar `i`, only bars `[0..i]` are visible to the
regime/strategy logic (achieved by literally slicing `df.iloc[:i+1]` before
calling into Scanner 1/2, matching how live scanning only ever sees data up
to "now"). A signal computed from bar `i`'s close fills at bar `i+1`'s open
— a signal can't realistically fill at the same price it was decided on.

Known simplifications (see also CLAUDE.md):
  - Only TP1 is simulated as an exit target (not partial scaling out at TP1
    then trailing to TP2) — this keeps the walk-forward loop's position
    state trivial (flat or one open trade) while still testing the
    strategy's real entry/SL/TP1 logic.
  - If both the stop-loss and TP1 are touched within the same bar (a bar
    with a wide range), the stop-loss is assumed to have hit first. This is
    the conservative assumption — it can't be determined from OHLC bars
    alone without tick data, and assuming the win always resolves first
    would systematically overstate performance.
  - Equity is markout only at trade close (realized P&L), not marked to
    market intra-trade — fine for Sharpe/drawdown on the realized-trade
    equity curve, but means a backtest's drawdown won't reflect unrealized
    open-position swings.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from config import Settings, get_settings
from src.data_provider import OHLCVRequest, fetch_ohlcv
from src.models import RegimeResult, TradeDirection
from src.scanner_1_regime import _build_metrics, classify_regime
from src.scanner_2_strategy import generate_signal
from src.symbol_resolver import to_yfinance_ticker

logger = logging.getLogger(__name__)

MIN_WARMUP_BARS = 210  # enough for EMA200 + indicator warm-up, matches data_provider's default


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT_1 = "TAKE_PROFIT_1"
    END_OF_DATA = "END_OF_DATA"


@dataclass(frozen=True)
class Trade:
    symbol: str
    timeframe: str
    direction: TradeDirection
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    stop_loss_price: float
    take_profit_1_price: float
    exit_reason: ExitReason
    r_multiple: float  # realized P&L in units of planned risk; 1R = the stop distance


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    bars_tested: int
    trades: list[Trade] = field(repr=False)
    equity_curve: pd.Series = field(repr=False)
    total_trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_realized_r: float = 0.0

    def passes_acceptance_criteria(
        self, min_sharpe: float = 1.5, min_profit_factor: float = 1.3, max_drawdown_pct: float = 12.0
    ) -> bool:
        return (
            self.sharpe_ratio > min_sharpe
            and self.profit_factor > min_profit_factor
            and self.max_drawdown_pct < max_drawdown_pct
        )


def _resolve_eval_start(df: pd.DataFrame, lookback_months: int | None) -> int:
    if lookback_months is None:
        return MIN_WARMUP_BARS
    cutoff = df.index[-1] - pd.DateOffset(months=lookback_months)
    idx = int(df.index.searchsorted(cutoff))
    return max(idx, MIN_WARMUP_BARS)


def _close_trade(
    open_trade: dict[str, Any], exit_time, exit_price: float, exit_reason: ExitReason
) -> Trade:
    direction = open_trade["direction"]
    entry_price = open_trade["entry_price"]
    sl = open_trade["stop_loss_price"]
    risk_dist = abs(entry_price - sl)
    signed_move = (exit_price - entry_price) if direction == TradeDirection.BUY else (entry_price - exit_price)
    r_multiple = (signed_move / risk_dist) if risk_dist > 0 else 0.0

    return Trade(
        symbol=open_trade["symbol"],
        timeframe=open_trade["timeframe"],
        direction=direction,
        entry_time=open_trade["entry_time"],
        entry_price=entry_price,
        exit_time=exit_time,
        exit_price=exit_price,
        stop_loss_price=sl,
        take_profit_1_price=open_trade["take_profit_1_price"],
        exit_reason=exit_reason,
        r_multiple=round(r_multiple, 4),
    )


def run_backtest(
    symbol: str,
    timeframe: str,
    settings: Settings | None = None,
    param_overrides: dict[str, dict[str, Any]] | None = None,
    lookback_months: int | None = 6,
) -> BacktestResult | None:
    """Walk-forward backtest of `symbol`/`timeframe` over roughly the last
    `lookback_months` (None = use however much history the data provider
    returns, minus warm-up). Returns None if there isn't enough data.
    `param_overrides` (keyed by strategy name) lets a candidate parameter
    variant be tested without writing to `data/strategy_params.json` —
    see `strategy_evolver.py`.
    """
    settings = settings or get_settings()
    fetch_ticker = to_yfinance_ticker(symbol)
    request = OHLCVRequest(symbol=fetch_ticker, timeframe=timeframe)
    df = fetch_ohlcv(request)
    if df is None:
        logger.warning("backtest: no data available for %s/%s", symbol, timeframe)
        return None

    eval_start_idx = _resolve_eval_start(df, lookback_months)
    if eval_start_idx >= len(df) - 2:
        logger.warning(
            "backtest: not enough data for %s/%s (%d bars, need > %d)", symbol, timeframe, len(df), eval_start_idx + 2
        )
        return None

    open_trade: dict[str, Any] | None = None
    trades: list[Trade] = []
    equity = 1.0
    equity_curve: dict[Any, float] = {}
    risk_fraction = settings.risk_percent_per_trade / 100

    for i in range(eval_start_idx, len(df) - 1):
        current_bar = df.iloc[i]
        next_bar = df.iloc[i + 1]

        if open_trade is not None:
            direction = open_trade["direction"]
            sl = open_trade["stop_loss_price"]
            tp1 = open_trade["take_profit_1_price"]
            hi, lo = float(current_bar["High"]), float(current_bar["Low"])

            hit_sl = (lo <= sl) if direction == TradeDirection.BUY else (hi >= sl)
            hit_tp = (hi >= tp1) if direction == TradeDirection.BUY else (lo <= tp1)

            if hit_sl or hit_tp:
                # Conservative same-bar-ambiguity resolution: SL wins if both touched.
                exit_price, exit_reason = (sl, ExitReason.STOP_LOSS) if hit_sl else (tp1, ExitReason.TAKE_PROFIT_1)
                trade = _close_trade(open_trade, current_bar.name, exit_price, exit_reason)
                trades.append(trade)
                equity *= 1 + trade.r_multiple * risk_fraction
                open_trade = None

        equity_curve[current_bar.name] = equity

        if open_trade is None:
            bar_window = df.iloc[: i + 1]  # no lookahead: only bars up to and including i
            try:
                metrics = _build_metrics(bar_window)
                regime, confidence = classify_regime(metrics, settings)
            except Exception:
                continue

            regime_result = RegimeResult(symbol, timeframe, regime, metrics, confidence)
            signal = generate_signal(regime_result, bar_window, settings, param_overrides=param_overrides)
            if signal is not None:
                # Fill at the *next* bar's open — a signal decided on bar i's
                # close can't realistically fill at that same price.
                open_trade = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "direction": signal.direction,
                    "entry_price": float(next_bar["Open"]),
                    "entry_time": next_bar.name,
                    "stop_loss_price": signal.stop_loss_price,
                    "take_profit_1_price": signal.take_profit_1_price,
                }

    if open_trade is not None:
        last_bar = df.iloc[-1]
        trade = _close_trade(open_trade, last_bar.name, float(last_bar["Close"]), ExitReason.END_OF_DATA)
        trades.append(trade)
        equity *= 1 + trade.r_multiple * risk_fraction
        equity_curve[last_bar.name] = equity

    return _compute_result(symbol, timeframe, df, eval_start_idx, trades, equity_curve)


def _compute_result(
    symbol: str,
    timeframe: str,
    df: pd.DataFrame,
    eval_start_idx: int,
    trades: list[Trade],
    equity_curve: dict[Any, float],
) -> BacktestResult:
    start, end = df.index[eval_start_idx], df.index[-1]
    bars_tested = len(df) - eval_start_idx
    equity_series = pd.Series(equity_curve, dtype=float).sort_index()

    if not trades:
        return BacktestResult(
            symbol=symbol, timeframe=timeframe, start=start, end=end, bars_tested=bars_tested,
            trades=[], equity_curve=equity_series,
        )

    r_multiples = np.array([t.r_multiple for t in trades])
    wins = r_multiples[r_multiples > 0]
    losses = r_multiples[r_multiples <= 0]

    win_rate_pct = 100.0 * len(wins) / len(trades)
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(abs(losses.sum())) if len(losses) else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    daily_equity = equity_series.resample("1D").last().ffill().dropna()
    daily_returns = daily_equity.pct_change().dropna()
    if len(daily_returns) >= 2 and daily_returns.std() > 0:
        sharpe_ratio = float((daily_returns.mean() / daily_returns.std()) * np.sqrt(252))
    else:
        sharpe_ratio = 0.0

    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max
    max_drawdown_pct = float(abs(drawdown.min()) * 100) if len(drawdown) else 0.0

    return BacktestResult(
        symbol=symbol, timeframe=timeframe, start=start, end=end, bars_tested=bars_tested,
        trades=trades, equity_curve=equity_series,
        total_trades=len(trades),
        win_rate_pct=round(win_rate_pct, 2),
        profit_factor=round(profit_factor, 3) if np.isfinite(profit_factor) else profit_factor,
        sharpe_ratio=round(sharpe_ratio, 3),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        avg_realized_r=round(float(r_multiples.mean()), 3),
    )
