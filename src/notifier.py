"""Alert Dispatcher — formats and sends trade signals to Telegram/Discord/desktop.

Owns message formatting and delivery only. Never computes trading logic —
it receives a finished `TradeSignal` and turns it into text + HTTP calls.
Each channel is dispatched independently and failures are caught per-channel
so, e.g., a Telegram outage never blocks the Discord send.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import platform
import subprocess

import httpx

from config import Settings, get_settings
from src.models import MarketRegime, StrategyType, TradeDirection, TradeSignal
from src.symbol_resolver import instrument_type

logger = logging.getLogger(__name__)

# Public (imported by src/telegram_bot.py for the on-demand /scan report so
# both message formats stay in sync instead of duplicating these labels).
DIRECTION_EMOJI = {TradeDirection.BUY: "🟢", TradeDirection.SELL: "🔴"}

DIRECTION_LABELS = {
    TradeDirection.BUY: "شراء (BUY)",
    TradeDirection.SELL: "بيع (SELL)",
}

REGIME_LABELS = {
    MarketRegime.STRONG_TREND_BULLISH: "اتجاه صاعد قوي",
    MarketRegime.STRONG_TREND_BEARISH: "اتجاه هابط قوي",
    MarketRegime.RANGING_CONSOLIDATION: "تذبذب / مسار عرضي",
    MarketRegime.HIGH_VOLATILITY_EXPANSION: "توسع سعري عالي السيولة",
    MarketRegime.LOW_LIQUIDITY_CHOP: "تذبذب ضعيف السيولة",
}

STRATEGY_LABELS = {
    StrategyType.TREND_PULLBACK: "ارتداد مع الاتجاه (ملامسة EMA 20)",
    StrategyType.MEAN_REVERSION_FADE: "ارتداد للوسط السعري (BB + RSI)",
    StrategyType.VOLATILITY_BREAKOUT: "اختراق سعري (Donchian/Keltner)",
    StrategyType.NO_TRADE: "لا توجد صفقة",
}


def format_pair(symbol: str) -> str:
    clean = symbol.upper().replace("=X", "").replace("/", "")
    if len(clean) == 6:
        return f"{clean[:3]}/{clean[3:]}"
    return clean


def format_message(signal: TradeSignal) -> str:
    regime_label = REGIME_LABELS.get(signal.regime, signal.regime.value)
    strategy_label = STRATEGY_LABELS.get(signal.strategy, signal.strategy.value)
    direction_emoji = DIRECTION_EMOJI.get(signal.direction, "")
    direction_label = DIRECTION_LABELS.get(signal.direction, signal.direction.value)
    tf_label = signal.timeframe.upper()

    if instrument_type(signal.symbol) == "FX":
        sl_text = f"🛑 وقف الخسارة: {signal.stop_loss_price} (-{signal.stop_loss_pips:g} نقطة)"
        tp1_text = (
            f"🎯 الهدف الأول: {signal.take_profit_1_price} "
            f"(+{signal.take_profit_1_pips:g} نقطة | العائد 1:{signal.take_profit_1_rr:g})"
        )
        tp2_text = (
            f"🎯 الهدف الثاني: {signal.take_profit_2_price} "
            f"(+{signal.take_profit_2_pips:g} نقطة | العائد 1:{signal.take_profit_2_rr:g})"
        )
        size_text = f"💼 حجم العقد المقترح: {signal.risk.lot_size:g} لوت قياسي"
    else:
        unit = (
            "بيتكوين"
            if signal.symbol.upper().startswith("BTC")
            else "إيثيريوم"
            if signal.symbol.upper().startswith("ETH")
            else "أونصة"
            if instrument_type(signal.symbol) == "METAL"
            else "وحدة"
        )
        sl_text = f"🛑 وقف الخسارة: {signal.stop_loss_price} (مسافة سعرية {signal.stop_loss_pips:g})"
        tp1_text = (
            f"🎯 الهدف الأول: {signal.take_profit_1_price} "
            f"(مسافة سعرية {signal.take_profit_1_pips:g} | العائد 1:{signal.take_profit_1_rr:g})"
        )
        tp2_text = (
            f"🎯 الهدف الثاني: {signal.take_profit_2_price} "
            f"(مسافة سعرية {signal.take_profit_2_pips:g} | العائد 1:{signal.take_profit_2_rr:g})"
        )
        size_text = f"💼 الحجم التقريبي: {signal.risk.lot_size:g} {unit} (قد يختلف حجم العقد حسب الوسيط)"

    lines = [
        "🚨 توصية تداول احترافية جديدة 🚨",
        f"📌 الزوج: {format_pair(signal.symbol)}",
        f"📈 نوع الصفقة: {direction_label} {direction_emoji}",
        f"📊 حالة السوق: {regime_label} ({tf_label})",
        f"🧠 الاستراتيجية: {strategy_label}",
        "------------------------------",
        f"🔹 سعر الدخول: {signal.entry_price}",
        sl_text,
        tp1_text,
        tp2_text,
        f"{size_text} (إدارة المخاطر: {signal.risk.risk_percent:g}% من رصيد ${signal.risk.account_size_usd:,.0f})",
        "------------------------------",
        f"🕒 الوقت: {signal.timestamp.strftime('%Y-%m-%d %H:%M:%S')} بتوقيت UTC",
    ]
    return "\n".join(lines)


def _get_target_telegram_id(settings: Settings) -> str:
    """يرجع معرف القناة إذا كان مضافاً، وإلا يرجع معرف المحادثة الافتراضي."""
    channel_id = getattr(settings, "telegram_channel_id", "")
    if channel_id and str(channel_id).strip():
        return str(channel_id).strip()
    return str(settings.telegram_chat_id).strip()


def send_telegram(message: str, settings: Settings) -> bool:
    target_chat_id = _get_target_telegram_id(settings)
    if not settings.telegram_bot_token or not target_chat_id:
        logger.debug("telegram not configured, skipping")
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        resp = httpx.post(
            url, json={"chat_id": target_chat_id, "text": message}, timeout=10.0
        )
        if resp.status_code == 429:
            logger.warning("telegram rate-limited (HTTP %s)", resp.status_code)
            return False
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("telegram send failed (%s)", type(exc).__name__)
        return False


def send_discord(message: str, settings: Settings) -> bool:
    if not settings.discord_webhook_url:
        logger.debug("discord not configured, skipping")
        return False
    try:
        resp = httpx.post(settings.discord_webhook_url, json={"content": message}, timeout=10.0)
        if resp.status_code == 429:
            logger.warning("discord rate-limited (HTTP %s)", resp.status_code)
            return False
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("discord send failed (%s)", type(exc).__name__)
        return False


def _escape_applescript_string(value: str) -> str:
    """Escape untrusted text before embedding it in an AppleScript literal."""
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return value.replace("\r", " ").replace("\n", " ").replace("\x00", " ")


def send_desktop(message: str, settings: Settings) -> bool:
    if not settings.enable_desktop_notifications:
        return False
    try:
        if platform.system() == "Darwin":
            title = "توصية فوركس"
            safe_message = _escape_applescript_string(message[:200])
            safe_title = _escape_applescript_string(title)
            script = f'display notification "{safe_message}" with title "{safe_title}"'
            subprocess.run(["osascript", "-e", script], check=True, timeout=5)
            return True
        logger.debug("desktop notifications only implemented for macOS in this build")
        return False
    except Exception as exc:
        logger.error("desktop notification failed (%s)", type(exc).__name__)
        return False


def dispatch_message(message: str, settings: Settings | None = None) -> dict[str, bool]:
    """يرسل أي رسالة نصية (توصية أو تنبيه حالة) إلى جميع القنوات المفعلة بأمان."""
    settings = settings or get_settings()
    results: dict[str, bool] = {}
    channels = (
        ("telegram", send_telegram),
        ("discord", send_discord),
        ("desktop", send_desktop),
    )
    for name, dispatcher in channels:
        try:
            results[name] = bool(dispatcher(message, settings))
        except Exception as exc:
            logger.error("%s notification failed (%s)", name, type(exc).__name__)
            results[name] = False
    return results


def send_alert(signal: TradeSignal, settings: Settings | None = None) -> dict[str, bool]:
    """Format the signal once and dispatch to every channel independently.
    Returns a per-channel success map; never raises."""
    settings = settings or get_settings()
    message = format_message(signal)
    results = dispatch_message(message, settings)
    if not any(results.values()):
        logger.warning(
            "no notification channel is configured/succeeded for %s %s signal",
            signal.symbol, signal.direction.value,
        )
    return results


def send_no_signal_alert(
    scanned_pairs_count: int = 0, settings: Settings | None = None
) -> dict[str, bool]:
    """يرسل تنبيهاً للقناة في حال وجود تذبذب أو عدم توفر توصيات عالية الدقة."""
    settings = settings or get_settings()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "⚠️ تنبيه الفحص الدوري للسوق ⚠️",
        "📊 حالة السوق: تذبذب / لا توجد فرص مكتملة الشروط حالياً.",
        f"🔍 تم فحص ({scanned_pairs_count}) من الأزواج والأطر الزمنية بدقة عالية.",
        "🛡️ النتيجة: لم يتم إصدار توصية في هذه الساعة حفاظاً على رأس المال من تذبذب السوق.",
        f"⏳ الفحص القادم سيتم تلقائياً وفق الجدول (كل {settings.auto_scan_interval_label_ar}).",
        "------------------------------",
        f"🕒 الوقت: {now_str} بتوقيت UTC",
    ]
    return dispatch_message("\n".join(lines), settings)


def send_news_pause_alert(
    news_details: str = "", settings: Settings | None = None
) -> dict[str, bool]:
    """يرسل تنبيهاً للقناة عند توقف البوت مؤقتاً بسبب الأخبار الاقتصادية."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "🛑 تنبيه هام: توقف مؤقت للبوت بسبب الأخبار 🛑",
        "📰 السبب: رصد أخبار اقتصادية قوية مؤثرة على حركة السوق حالياً.",
    ]
    if news_details:
        lines.append(f"📌 تفاصيل الخبر: {news_details}")
    lines.extend([
        "🛡️ الإجراء: تم إيقاف إرسال التوصيات مؤقتاً لتجنب الانزلاقات السعرية والتذبذب العنيف.",
        "🔄 سيعود البوت للعمل وإرسال التوصيات تلقائياً فور انتهاء تأثير الأخبار.",
        "------------------------------",
        f"🕒 الوقت: {now_str} بتوقيت UTC",
    ])
    return dispatch_message("\n".join(lines), settings)


def send_news_resume_alert(settings: Settings | None = None) -> dict[str, bool]:
    """يرسل تنبيهاً للقناة فور انتهاء الأخبار وعودة البوت للعمل."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "✅ تنبيه: عودة البوت للعمل تلقائياً ✅",
        "📈 انتهت فترة الأخبار الاقتصادية المؤثرة وبدأت حركة السوق بالاستقرار.",
        "🚀 استأنف البوت عمليات الفحص الدوري لاقتناص التوصيات الاحترافية.",
        "------------------------------",
        f"🕒 الوقت: {now_str} بتوقيت UTC",
    ]
    return dispatch_message("\n".join(lines), settings)
