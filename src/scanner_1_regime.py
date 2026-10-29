"""Scanner 1 — Market Flow & Regime Analyzer (Enhanced Institutional Edition).

Classifies each (symbol, timeframe) pair into one of five `MarketRegime`
states using trend strength (ADX + EMA alignment + EMA slope + swing structure),
price efficiency (anti-chop filter), volatility (ATR expansion, Bollinger
bandwidth), and liquidity/session flow (RVOL, active FX session).

Public entry points: `scan_regime`, `scan_watchlist`, `scan_regime_with_data`,
`scan_watchlist_with_data`, `scan_symbols_with_data`.
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
EFFICIENCY_LOOKBACK = 10


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
    """Rule-based institutional regime classifier. Returns (regime, confidence 0..1).

    Evaluates ADX strength, EMA stack alignment, price-to-EMA50 positioning,
    ATR-normalized EMA separation, relative volume (RVOL), and volatility structure.
    """
    adx_strong = metrics.adx > settings.adx_trend_threshold
    ema_bull_aligned = metrics.ema_20 > metrics.ema_50 > metrics.ema_200
    ema_bear_aligned = metrics.ema_20 < metrics.ema_50 < metrics.ema_200
    atr_ratio = metrics.atr / metrics.atr_avg if metrics.atr_avg > 0 else 1.0

    # قياس تباعد المتوسطات نسبةً إلى حركة السعر (ATR) للتأكد من عدم تشابك المتوسطات
    ema_spread_atr = (
        abs(metrics.ema_20 - metrics.ema_50) / metrics.atr if metrics.atr > 0 else 0.5
    )
    spread_bonus = min(0.08, max(0.0, (ema_spread_atr - 0.3) * 0.1))

    # مكافأة أو خصم حسب السيولة الفعلية (RVOL)
    if metrics.rvol >= 0.9:
        rvol_adj = 0.05
    elif metrics.rvol < LOW_RVOL_THRESHOLD:
        rvol_adj = -0.10
    else:
        rvol_adj = 0.0

    # 1. الاتجاه الصاعد القوي المكتمل (ADX + ترتيب المتوسطات + قمم صاعدة + السعر فوق EMA50)
    if adx_strong and ema_bull_aligned and metrics.higher_highs:
        price_holds_trend = metrics.close >= metrics.ema_50
        base_conf = (metrics.adx / 50.0) * 0.6 + 0.4
        if not price_holds_trend:
            base_conf -= 0.12
        confidence = min(1.0, max(0.45, base_conf + spread_bonus + rvol_adj))
        return MarketRegime.STRONG_TREND_BULLISH, confidence

    # 2. الاتجاه الهابط القوي المكتمل (ADX + ترتيب المتوسطات + قيعان هابطة + السعر تحت EMA50)
    if adx_strong and ema_bear_aligned and metrics.lower_lows:
        price_holds_trend = metrics.close <= metrics.ema_50
        base_conf = (metrics.adx / 50.0) * 0.6 + 0.4
        if not price_holds_trend:
            base_conf -= 0.12
        confidence = min(1.0, max(0.45, base_conf + spread_bonus + rvol_adj))
        return MarketRegime.STRONG_TREND_BEARISH, confidence

    # 3. اتجاه صاعد أو هابط بدون اكتمال هيكل القمم/القيعان (ثقة متوسطة لا تُرسل إلا إذا تأكدت)
    if adx_strong and ema_bull_aligned:
        return MarketRegime.STRONG_TREND_BULLISH, 0.55

    if adx_strong and ema_bear_aligned:
        return MarketRegime.STRONG_TREND_BEARISH, 0.55

    # 4. توسع سعري عالي السيولة (مناسب لاستراتيجيات الاختراق القوية)
    if atr_ratio >= settings.atr_expansion_multiplier or metrics.bb_bandwidth_percentile >= 80:
        raw_conf = atr_ratio / (settings.atr_expansion_multiplier * 1.35)
        if metrics.rvol >= 1.1 and adx_strong:
            raw_conf += 0.10
        confidence = min(1.0, max(0.5, raw_conf))
        return MarketRegime.HIGH_VOLATILITY_EXPANSION, confidence

    # 5. انضغاط سعري / مسار عرضي واضح (مناسب لاستراتيجية الارتداد للوسط)
    if metrics.bb_bandwidth_percentile <= settings.bb_squeeze_percentile and not adx_strong:
        squeeze_quality = 1.0 - (metrics.bb_bandwidth_percentile / settings.bb_squeeze_percentile) * 0.4
        confidence = min(0.95, max(0.5, squeeze_quality))
        return MarketRegime.RANGING_CONSOLIDATION, confidence

    # 6. تذبذب ضعيف السيولة (يُمنع التداول فيه)
    if metrics.rvol < LOW_RVOL_THRESHOLD:
        return MarketRegime.LOW_LIQUIDITY_CHOP, 0.6

    # 7. تذبذب عادي غير واضح المعالم
    return MarketRegime.RANGING_CONSOLIDATION, 0.45


def _refine_confidence_with_price_action(
    regime: MarketRegime,
    confidence: float,
    df: pd.DataFrame,
    metrics: StructuralMetrics,
) -> tuple[MarketRegime, float]:
    """يرفع دقة الفحص عبر تحليل كفاءة حركة الشموع وميل المتوسطات المتحركة (EMA Slope)."""
    try:
        if len(df) < 25:
            return regime, confidence

        close = df["Close"]
        ema20 = ind.ema(close, 20)
        ema50 = ind.ema(close, 50)

        # 1. حساب نسبة كفاءة الحركة السعرية (Kaufman Efficiency Ratio) لآخر 10 شموع
        # تقيس مدى استقامة الحركة مقارنة بالتذبذب العشوائي
        net_change = abs(float(close.iloc[-1]) - float(close.iloc[-EFFICIENCY_LOOKBACK]))
        sum_changes = float(close.diff().abs().tail(EFFICIENCY_LOOKBACK).sum())
        efficiency_ratio = net_change / sum_changes if sum_changes > 0 else 0.0

        # 2. حساب ميل المتوسطات المتحركة لآخر 3 شموع
        ema20_slope = float(ema20.iloc[-1]) - float(ema20.iloc[-4])
        ema50_slope = float(ema50.iloc[-1]) - float(ema50.iloc[-4])

        if regime == MarketRegime.STRONG_TREND_BULLISH:
            # في الاتجاه الصاعد: يجب أن يكون ميل المتوسطات صاعداً
            if ema20_slope > 0 and ema50_slope > 0:
                confidence += 0.05
            elif ema20_slope < 0 and ema50_slope <= 0:
                confidence -= 0.12

            # مكافأة الحركة النقية ومعاقبة التذبذب الحاد
            if efficiency_ratio >= 0.40:
                confidence += 0.04
            elif efficiency_ratio < 0.18:
                confidence -= 0.10

        elif regime == MarketRegime.STRONG_TREND_BEARISH:
            # في الاتجاه الهابط: يجب أن يكون ميل المتوسطات هابطاً
            if ema20_slope < 0 and ema50_slope < 0:
                confidence += 0.05
            elif ema20_slope > 0 and ema50_slope >= 0:
                confidence -= 0.12

            if efficiency_ratio >= 0.40:
                confidence += 0.04
            elif efficiency_ratio < 0.18:
                confidence -= 0.10

        confidence = min(1.0, max(0.1, round(confidence, 4)))
        return regime, confidence
    except Exception:
        return regime, confidence


def scan_regime_with_data(
    symbol: str, timeframe: str, settings: Settings | None = None, fetch_symbol: str | None = None
) -> tuple[RegimeResult, pd.DataFrame] | None:
    """Fetch data, classify the regime, and return both the result and the
    underlying OHLCV frame."""
    settings = settings or get_settings()
    request = OHLCVRequest(symbol=fetch_symbol or symbol, timeframe=timeframe)

    df = fetch_ohlcv(request)
    if df is None:
        logger.warning("skipping %s/%s: no data available", symbol, timeframe)
        return None

    try:
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
        regime, confidence = _refine_confidence_with_price_action(
            regime, confidence, df, metrics
        )
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
    static `.env` WATCHLIST."""
    settings = settings or get_settings()
    results: list[tuple[RegimeResult, pd.DataFrame]] = []
    for symbol in symbols:
        fetch_ticker = to_yfinance_ticker(symbol)
        for timeframe in settings.timeframe_list:
            outcome = scan_regime_with_data(symbol, timeframe, settings, fetch_symbol=fetch_ticker)
            if outcome is not None:
                results.append(outcome)
    return results
