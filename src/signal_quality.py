"""Transparent rule-based signal-quality checklist (not a win probability)."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import Settings
from src.models import MarketRegime, StrategyType, TradeDirection, TradeSignal


@dataclass(frozen=True)
class SignalQuality:
    score: int
    grade: str
    eligible: bool
    reasons: tuple[str, ...]


def assess_signal_quality(
    signal: TradeSignal,
    regime_confidence: float,
    regime: MarketRegime,
    atr: float,
    df: pd.DataFrame,
    settings: Settings,
) -> SignalQuality:
    """Score observed checklist conditions; this score is not calibrated to outcomes."""
    score = 0
    reasons: list[str] = []
    confidence = min(1.0, max(0.0, float(regime_confidence)))
    score += round(30 * confidence)
    reasons.append(f"ثقة قواعد النظام {confidence:.2f}/1")

    aligned = (
        (signal.strategy == StrategyType.TREND_PULLBACK and (
            (signal.direction == TradeDirection.BUY and regime == MarketRegime.STRONG_TREND_BULLISH)
            or (signal.direction == TradeDirection.SELL and regime == MarketRegime.STRONG_TREND_BEARISH)
        ))
        or (signal.strategy == StrategyType.MEAN_REVERSION_FADE and regime == MarketRegime.RANGING_CONSOLIDATION)
        or (signal.strategy == StrategyType.VOLATILITY_BREAKOUT and regime == MarketRegime.HIGH_VOLATILITY_EXPANSION)
    )
    if aligned:
        score += 25
        reasons.append("الاستراتيجية متوافقة مع نظام السوق")

    if signal.take_profit_1_rr >= settings.min_risk_reward:
        score += 25
        reasons.append(f"العائد/المخاطرة {signal.take_profit_1_rr:.2f} يحقق الحد الأدنى")

    stop_distance = abs(float(signal.entry_price) - float(signal.stop_loss_price))
    if atr > 0:
        stop_ratio = stop_distance / float(atr)
        if 0.25 <= stop_ratio <= 3.0:
            score += 15
            reasons.append("مسافة الوقف ضمن نطاق الحركة الحديثة")
        elif 0.15 <= stop_ratio <= 4.5:
            score += 7
            reasons.append("مسافة الوقف مقبولة لكنها ليست مثالية")
    else:
        reasons.append("ATR غير صالح؛ لم تُمنح نقاط لمسافة الوقف")

    if len(df) >= 2 and "Close" in df.columns:
        close_now = float(df["Close"].iloc[-1])
        close_prev = float(df["Close"].iloc[-2])
        momentum_ok = (
            (signal.direction == TradeDirection.BUY and close_now >= close_prev)
            or (signal.direction == TradeDirection.SELL and close_now <= close_prev)
        )
        if momentum_ok:
            score += 5
            reasons.append("إغلاق آخر شمعة لا يعاكس اتجاه الإشارة")

    score = max(0, min(100, int(score)))
    grade = "A" if score >= 85 else "B" if score >= 70 else "C"
    confidence_ok = confidence >= settings.min_signal_confidence
    minimum_score = int(getattr(settings, "min_signal_quality_score", 70))
    eligible = score >= minimum_score and confidence_ok
    if not confidence_ok:
        reasons.append("ثقة النظام دون الحد الأدنى المضبوط")
    if score < minimum_score:
        reasons.append("الدرجة دون حد النشر")
    return SignalQuality(score, grade, eligible, tuple(reasons))
