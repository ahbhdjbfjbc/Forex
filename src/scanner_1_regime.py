"""Scanner 1 — Market Flow & Regime Analyzer.

Classifies each (symbol, timeframe) pair into one of five `MarketRegime`
states using trend strength (ADX + EMA alignment + swing structure),
volatility (ATR expansion, Bollinger bandwidth), and liquidity/session flow
(RVOL, active FX session).

Public entry points: `scan_regime`, `scan_watchlist`.
"""
from __future__ import annotations

import logging

import pandas as pd

from config import Settings, get_settings
from src import indicators as ind
from src.data_provider import OHLCVRequest, fetch_ohlcv
from src.models import MarketRegime, RegimeResult, StructuralMetrics
from src.session_utils import compute_rvol, get_forex_session
from src.symbol_resolver import to_yfinance_ticker

logger = logging.getLogger(__name__)

ATR_LOOKBACK_FOR_AVG = 50
BB_LENGTH = 20
ADX_LENGTH = 14
STRUCTURE_LOOKBACK = 50
LOW_RVOL_THRESHOLD = 0.5


def _build_metrics(df: pd.DataFrame) -> StructuralMetrics:
    adx_df = ind.adx(df, ADX_LENGTH)
    ema20 = ind.ema(df["Close"], 20)
    ema50 = ind.ema(df["Close"], 50)
    ema200 = ind.ema(df["Close"], 200)
    atr_series = ind.atr(df, 14)
    bb = ind.bollinger_bands(df["Close"], BB_LENGTH)
    structure = ind.detect_structure(df, STRUCTURE_LOOKBACK)

    bandwidth_pct = ind.bandwidth_percentile_rank(bb["BANDWIDTH"], lookback=100)
    atr_avg = atr_series.tail(ATR_LOOKBACK_FOR_AVG).mean()
    rvol = compute_rvol(df["Volume"])

    return StructuralMetrics(
        adx=float(adx_df["ADX"].iloc[-1]),
        ema_20=float(ema20.iloc[-1]),
        ema_50=float(ema50.iloc[-1]),
        ema_200=float(ema200.iloc[-1]),
        higher_highs=structure["higher_highs"],
        lower_lows=structure["lower_lows"],
        atr=float(atr_series.iloc[-1]),
        atr_avg=float(atr_avg) if pd.notna(atr_avg) else float(atr_series.iloc[-1]),
        bb_bandwidth=float(bb["BANDWIDTH"].iloc[-1]) if pd.notna(bb["BANDWIDTH"].iloc[-1]) else 0.0,
        bb_bandwidth_percentile=bandwidth_pct,
        rvol=rvol,
        session=get_forex_session(),
        close=float(df["Close"].iloc[-1]),
    )


def classify_regime(
    metrics: StructuralMetrics, settings: Settings
) -> tuple[MarketRegime, float]:
    """Rule-based regime classifier. Returns (regime, confidence 0..1).

    Priority order matters: strong trend is checked first (it can coexist
    with high volatility, and trend should win), then volatility expansion,
    then consolidation, then low-liquidity chop as the catch-all.
    """
    adx_strong = metrics.adx > settings.adx_trend_threshold
    ema_bull_aligned = metrics.ema_20 > metrics.ema_50 > metrics.ema_200
    ema_bear_aligned = metrics.ema_20 < metrics.ema_50 < metrics.ema_200
    atr_ratio = metrics.atr / metrics.atr_avg if metrics.atr_avg > 0 else 1.0

    if adx_strong and ema_bull_aligned and metrics.higher_highs:
        confidence = min(1.0, (metrics.adx / 50) * 0.6 + 0.4)
        return MarketRegime.STRONG_TREND_BULLISH, confidence

    if adx_strong and ema_bear_aligned and metrics.lower_lows:
        confidence = min(1.0, (metrics.adx / 50) * 0.6 + 0.4)
        return MarketRegime.STRONG_TREND_BEARISH, confidence

    if adx_strong and ema_bull_aligned:
        return MarketRegime.STRONG_TREND_BULLISH, 0.5

    if adx_strong and ema_bear_aligned:
        return MarketRegime.STRONG_TREND_BEARISH, 0.5

    if atr_ratio >= settings.atr_expansion_multiplier or metrics.bb_bandwidth_percentile >= 80:
        confidence = min(1.0, atr_ratio / (settings.atr_expansion_multiplier * 1.5))
        return MarketRegime.HIGH_VOLATILITY_EXPANSION, max(confidence, 0.5)

    if metrics.bb_bandwidth_percentile <= settings.bb_squeeze_percentile and not adx_strong:
        confidence = 1.0 - (metrics.bb_bandwidth_percentile / settings.bb_squeeze_percentile) * 0.5
        return MarketRegime.RANGING_CONSOLIDATION, max(confidence, 0.5)

    if metrics.rvol < LOW_RVOL_THRESHOLD:
        return MarketRegime.LOW_LIQUIDITY_CHOP, 0.6

    return MarketRegime.RANGING_CONSOLIDATION, 0.4


def scan_regime_with_data(
    symbol: str, timeframe: str, settings: Settings | None = None, fetch_symbol: str | None = None
) -> tuple[RegimeResult, pd.DataFrame] | None:
    """Fetch data, classify the regime, and return both the result and the
    underlying OHLCV frame. Scanner 2 needs the same bars to compute exact
    entry/SL/TP levels — this lets the orchestrator (main.py) fetch once and
    pass the frame through, instead of Scanner 2 calling the data provider
    itself. Returns None if data is unavailable.

    `fetch_symbol` lets a caller decouple "what to fetch" from "what to call
    it": Yahoo Finance has no XAUUSD/XAGUSD ticker, so the on-demand Telegram
    scanner fetches "GC=F"/"SI=F" while keeping the canonical "XAUUSD" as the
    `symbol` identity that flows through RegimeResult/TradeSignal (needed so
    `src.symbol_resolver.instrument_type` and display formatting stay
    correct). Defaults to `symbol` for every other caller.
    """
    settings = settings or get_settings()
    request = OHLCVRequest(symbol=fetch_symbol or symbol, timeframe=timeframe)

    df = fetch_ohlcv(request)
    if df is None:
        logger.warning("skipping %s/%s: no data available", symbol, timeframe)
        return None

    try:
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
    except Exception:
        logger.exception("regime classification failed for %s/%s", symbol, timeframe)
        return None

    result = RegimeResult(
        symbol=symbol,
        timeframe=timeframe,
        regime=regime,
        metrics=metrics,
        confidence=confidence,
    )
    return result, df


def scan_regime(symbol: str, timeframe: str, settings: Settings | None = None) -> RegimeResult | None:
    """Convenience wrapper over `scan_regime_with_data` for callers (tests,
    ad-hoc scripts) that only need the classification, not the raw frame."""
    outcome = scan_regime_with_data(symbol, timeframe, settings)
    return outcome[0] if outcome is not None else None


def scan_watchlist(settings: Settings | None = None) -> list[RegimeResult]:
    """Scan every (symbol, timeframe) combination in the configured
    watchlist. Failures on individual pairs are logged and skipped —
    one bad symbol never aborts the full scan."""
    return [result for result, _df in scan_watchlist_with_data(settings)]


def scan_watchlist_with_data(
    settings: Settings | None = None,
) -> list[tuple[RegimeResult, pd.DataFrame]]:
    """Same as `scan_watchlist` but also returns each OHLCV frame, for
    orchestrators that need to hand data through to Scanner 2."""
    settings = settings or get_settings()
    results: list[tuple[RegimeResult, pd.DataFrame]] = []
    for symbol in settings.watchlist_symbols:
        for timeframe in settings.timeframe_list:
            outcome = scan_regime_with_data(symbol, timeframe, settings)
            if outcome is not None:
                results.append(outcome)
    return results


def scan_symbols_with_data(
    symbols: list[str], settings: Settings | None = None
) -> list[tuple[RegimeResult, pd.DataFrame]]:
    """Like `scan_watchlist_with_data`, but over an explicit, arbitrary list
    of canonical symbols (e.g. from `src.watchlist_store`) instead of the
    static `.env` WATCHLIST. Each symbol's `fetch_symbol` is resolved via
    `symbol_resolver.to_yfinance_ticker`, so canonical forms like "XAUUSD"
    or "BTCUSD" work here the same way they do for the on-demand scanner."""
    settings = settings or get_settings()
    results: list[tuple[RegimeResult, pd.DataFrame]] = []
    for symbol in symbols:
        fetch_ticker = to_yfinance_ticker(symbol)
        for timeframe in settings.timeframe_list:
            outcome = scan_regime_with_data(symbol, timeframe, settings, fetch_symbol=fetch_ticker)
            if outcome is not None:
                results.append(outcome)
    return results
