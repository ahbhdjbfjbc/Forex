"""Arabic aggregate status messages for automated market scans."""
from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from src.models import MarketRegime
from src.notifier import REGIME_LABELS

DEFAULT_NEWS_STATUS = (
    "الأخبار: لم يُفعّل فلتر تقويم اقتصادي حي بعد؛ "
    "يعتمد التحليل حالياً على البيانات الفنية فقط."
)


class MarketCycleSummary(NamedTuple):
    """Immutable counts and scan outcomes used to report one scheduler cycle."""

    symbols_count: int
    timeframe_count: int
    data_pairs: int
    qualified_signals: int
    delivered_signals: int
    low_confidence: int
    no_entry: int
    failed_deliveries: int
    confidence_threshold: float
    regime_counts: Mapping[MarketRegime, int]


def format_market_status(
    summary: MarketCycleSummary,
    news_status: str = DEFAULT_NEWS_STATUS,
) -> str:
    """Return one concise report without claiming price/news data that is absent."""
    expected_pairs = summary.symbols_count * summary.timeframe_count
    lines = [
        "📋 ملخص دورة فحص السوق",
        f"الرموز المراقبة: {summary.symbols_count} | "
        f"الأطر الزمنية: {summary.timeframe_count}",
        f"بيانات السوق المتاحة: {summary.data_pairs}/{expected_pairs}",
        f"التوصيات المؤهلة: {summary.qualified_signals} | "
        f"التي وصل إرسالها: {summary.delivered_signals}",
    ]

    if summary.no_entry:
        breakdown = "؛ ".join(
            f"{REGIME_LABELS.get(regime, regime.value)}: {count}"
            for regime, count in sorted(
                summary.regime_counts.items(), key=lambda item: item[0].value
            )
            if count
        )
        lines.append(
            f"لا توجد نقطة دخول مؤكدة في {summary.no_entry} حالة؛ "
            "انتظر تأكيداً جديداً."
        )
        if breakdown:
            lines.append(f"حالات السوق التي لم تعطِ دخولاً: {breakdown}.")

    if summary.low_confidence:
        lines.append(
            f"استُبعدت {summary.low_confidence} إشارة لأن الثقة أقل من "
            f"الحد المحدد ({summary.confidence_threshold:.0%})."
        )

    missing_pairs = max(0, expected_pairs - summary.data_pairs)
    if missing_pairs:
        lines.append(
            f"تعذر جلب بيانات {missing_pairs} حالة؛ "
            "لا يمكن تأكيد وضعها من هذه الدورة."
        )

    if summary.failed_deliveries:
        lines.append(
            f"⚠️ تعذر تأكيد إرسال {summary.failed_deliveries} توصية مؤهلة؛ "
            "راجع إعدادات قنوات التنبيه وسجلات الخدمة."
        )
    elif summary.qualified_signals == 0:
        lines.append("لا توجد توصية جديدة في هذه الدورة؛ انتظر المسح التالي.")
    elif summary.delivered_signals:
        lines.append("اكتملت الدورة ووصلت التوصيات المؤهلة إلى قناة التنبيه المتاحة.")

    lines.append(news_status)
    return "\n".join(lines)
