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
import threading
from pathlib import Path

import httpx

from config import Settings, get_settings
from src.models import MarketRegime, StrategyType, TradeDirection, TradeSignal
from src.signal_lifecycle import SignalOutcome, signal_lifecycle
from src.symbol_resolver import instrument_type

logger = logging.getLogger(__name__)

# The channel message ID of the most recent successful sendPhoto on this
# thread, so send_alert can persist it and reply to it with the outcome.
_delivery_state = threading.local()


class DeliveryResult(dict):
    """Per-channel delivery flags plus an optional publish-gate block reason.

    Behaves exactly like the original dict for existing callers; a blocked
    publication has every channel False and `blocked_reason` set."""

    blocked_reason: str | None = None

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
    StrategyType.EXTERNAL_ALERT: "تنبيه خارجي من TradingView (غير مصنف باستراتيجية داخلية)",
    StrategyType.NO_TRADE: "لا توجد صفقة",
}


def format_pair(symbol: str) -> str:
    clean = symbol.upper().replace("=X", "").replace("/", "")
    if len(clean) == 6:
        return f"{clean[:3]}/{clean[3:]}"
    return clean


def format_message(signal: TradeSignal, market_frame=None) -> str:
    regime_label = REGIME_LABELS.get(signal.regime, signal.regime.value)
    strategy_label = STRATEGY_LABELS.get(signal.strategy, signal.strategy.value)
    direction_emoji = DIRECTION_EMOJI.get(signal.direction, "")
    direction_label = DIRECTION_LABELS.get(signal.direction, signal.direction.value)
    tf_label = signal.timeframe.upper()

    if instrument_type(signal.symbol) == "FX":
        sl_text = f"🛑 وقف الخسارة: {signal.stop_loss_price} (-{signal.stop_loss_pips:g} نقطة)"
        target_text = (
            f"🎯 الهدف الوحيد: {signal.take_profit_2_price} "
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
        target_text = (
            f"🎯 الهدف الوحيد: {signal.take_profit_2_price} "
            f"(مسافة سعرية {signal.take_profit_2_pips:g} | العائد 1:{signal.take_profit_2_rr:g})"
        )
        size_text = f"💼 الحجم التقريبي: {signal.risk.lot_size:g} {unit} (قد يختلف حجم العقد حسب الوسيط)"

    context_lines = []
    if market_frame is not None and len(market_frame) > 1:
        prior = market_frame.iloc[:-1].tail(50)
        if not prior.empty:
            support = float(prior["Low"].min())
            resistance = float(prior["High"].max())
            context_lines.append(f"📐 دعم/مقاومة سابقة: {support:g} / {resistance:g}")
    if signal.symbol.upper() == "XAUUSD":
        context_lines.append("⚠️ مصدر الذهب: GC=F (عقود COMEX الآجلة)، وليس سعراً فورياً XAUUSD")

    lines = [
        "🚨 توصية تداول احترافية جديدة 🚨",
        f"📌 الزوج: {format_pair(signal.symbol)}",
        f"📈 نوع الصفقة: {direction_label} {direction_emoji}",
        f"📊 حالة السوق: {regime_label} ({tf_label})",
        f"🧠 الاستراتيجية: {strategy_label}",
        *(
            [f"🧮 تقييم شروط الفرصة: {signal.quality_score}/100 ({signal.quality_grade}) — ليس نسبة نجاح"]
            if signal.quality_score is not None and signal.quality_grade
            else []
        ),
        "------------------------------",
        f"🔹 سعر الدخول: {signal.entry_price}",
        sl_text,
        target_text,
        *context_lines,
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


def send_telegram(message: str, settings: Settings, *, reply_to_message_id: int | None = None) -> bool:
    target_chat_id = _get_target_telegram_id(settings)
    if not settings.telegram_bot_token or not target_chat_id:
        logger.debug("telegram not configured, skipping")
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    body = {"chat_id": target_chat_id, "text": message}
    if reply_to_message_id:
        # Thread the result under the original recommendation; still send if
        # the original post was deleted.
        body["reply_parameters"] = {
            "message_id": int(reply_to_message_id),
            "allow_sending_without_reply": True,
        }
    try:
        resp = httpx.post(url, json=body, timeout=10.0)
        if resp.status_code == 429:
            logger.warning("telegram rate-limited (HTTP %s)", resp.status_code)
            return False
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("telegram send failed (%s)", type(exc).__name__)
        return False


def send_telegram_photo(caption: str, image_path: str | Path, settings: Settings) -> bool:
    """Send the recommendation and its chart in one Telegram sendPhoto call."""
    target_chat_id = _get_target_telegram_id(settings)
    path = Path(image_path)
    if not settings.telegram_bot_token or not target_chat_id or not path.is_file():
        logger.warning("telegram photo delivery unavailable (configuration or chart missing)")
        return False
    if path.stat().st_size > 8 * 1024 * 1024:
        logger.warning("telegram photo rejected because chart exceeds the configured size limit")
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
    try:
        with path.open("rb") as image_file:
            response = httpx.post(
                url,
                data={"chat_id": target_chat_id, "caption": caption[:1024]},
                files={"photo": (path.name, image_file, "image/png")},
                timeout=20.0,
            )
        if response.status_code == 429:
            logger.warning("telegram photo send rate-limited")
            return False
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            logger.warning("telegram API did not acknowledge photo delivery")
            return False
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        _delivery_state.last_photo_message_id = message_id if isinstance(message_id, int) else None
        return True
    except (httpx.HTTPError, OSError, ValueError) as exc:
        logger.error("telegram photo send failed (%s)", type(exc).__name__)
        return False


def send_admin_message(message: str, settings: Settings) -> bool:
    """Send an administrative report only to the configured numeric user ID."""
    admin_id = str(getattr(settings, "telegram_allowed_user_id", "") or "").strip()
    if not admin_id.isdecimal() or not settings.telegram_bot_token:
        logger.warning("private admin message unavailable (admin ID or bot token missing)")
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        response = httpx.post(
            url,
            json={"chat_id": admin_id, "text": str(message)[:4096]},
            timeout=15.0,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            logger.warning("telegram API did not acknowledge private admin report")
            return False
        return True
    except (httpx.HTTPError, OSError, ValueError) as exc:
        logger.error("private admin report failed (%s)", type(exc).__name__)
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


def dispatch_message(
    message: str, settings: Settings | None = None, *, reply_to_message_id: int | None = None
) -> dict[str, bool]:
    """يرسل أي رسالة نصية (توصية أو تنبيه حالة) إلى جميع القنوات المفعلة بأمان."""
    settings = settings or get_settings()
    results: dict[str, bool] = {}

    def telegram(text: str, cfg: Settings) -> bool:
        if reply_to_message_id:
            return send_telegram(text, cfg, reply_to_message_id=reply_to_message_id)
        return send_telegram(text, cfg)

    channels = (
        ("telegram", telegram),
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


def dispatch_signal(message: str, image_path: str | Path, settings: Settings | None = None) -> dict[str, bool]:
    """Send a chart+caption to Telegram, and text to optional other channels."""
    settings = settings or get_settings()
    results: dict[str, bool] = {}
    try:
        results["telegram"] = bool(send_telegram_photo(message, image_path, settings))
    except Exception as exc:
        logger.error("telegram photo notification failed (%s)", type(exc).__name__)
        results["telegram"] = False
    for name, dispatcher in (("discord", send_discord), ("desktop", send_desktop)):
        try:
            results[name] = bool(dispatcher(message, settings))
        except Exception as exc:
            logger.error("%s notification failed (%s)", name, type(exc).__name__)
            results[name] = False
    return results


def format_signal_outcome(outcome: SignalOutcome) -> str:
    """Format one terminal price outcome without implying broker execution."""
    result_labels = {
        "TAKE_PROFIT_2": "تحقق الهدف",
        "STOP_LOSS": "تحقق وقف الخسارة",
        "AMBIGUOUS": "نتيجة ملتبسة؛ لامست الشمعة الوقف والهدف ولا يمكن معرفة أيهما تحقق أولاً",
        "REVERSED": "أُغلقت التوصية عند ظهور إشارة معاكسة",
    }
    seconds = max(0, int(outcome.duration_seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    duration = " ".join(
        part for value, part in ((days, "يوم"), (hours, "ساعة"), (minutes, "دقيقة"))
        if value
    ) or "أقل من دقيقة"
    exit_label = f"{outcome.exit_price:g}" if outcome.exit_price is not None else "غير قابل للتحديد من OHLCV"
    points_label = (
        f"{outcome.points:+g} {outcome.points_unit} | المحصلة النظرية: {outcome.r_multiple:+g}R"
        if outcome.points is not None and outcome.r_multiple is not None
        else "غير محسوبة (ترتيب لمس المستويات مجهول)"
    )
    return (
        "📊 نتيجة التوصية (تقدير من شموع OHLCV؛ ليست إثبات تنفيذ لدى وسيط)\n"
        f"📌 {format_pair(outcome.symbol)} — {outcome.timeframe.upper()} — {outcome.direction} — {outcome.strategy}\n"
        f"📍 الدخول: {outcome.entry_price:g} | وقف الخسارة: {outcome.stop_loss_price:g} | الهدف: {outcome.target_price:g}\n"
        f"🚪 سعر الإغلاق: {exit_label}\n"
        f"🏁 النتيجة: {result_labels.get(outcome.outcome, outcome.outcome)}\n"
        f"📐 الحركة السعرية: {points_label}\n"
        f"⏱️ مدة التوصية: {duration}\n"
        f"🕒 وقت التحقق: {outcome.closed_at} UTC | المصدر: {outcome.source}\n"
        "ℹ️ مصدر OHLCV لا يوفر Bid/Ask؛ السبريد والانزلاق غير محتسبين، والنتيجة تقديرية.\n"
        f"🆔 رقم التوصية: {outcome.signal_id}"
    )


def send_signal_outcome(
    outcome: SignalOutcome, settings: Settings | None = None
) -> dict[str, bool]:
    """Dispatch a terminal result and acknowledge it in the durable outbox.

    When the channel message ID of the original recommendation is known, the
    result is posted as a reply to it so every trade and its outcome stay
    linked in the same channel."""
    try:
        original_message_id = signal_lifecycle.telegram_message_id(outcome.signal_id)
    except Exception:
        original_message_id = None
    if original_message_id:
        results = dispatch_message(
            format_signal_outcome(outcome), settings, reply_to_message_id=original_message_id
        )
    else:
        results = dispatch_message(format_signal_outcome(outcome), settings)
    if any(results.values()):
        signal_lifecycle.mark_outcome_sent(outcome.signal_id)
    return results


def flush_pending_signal_outcomes(settings: Settings | None = None) -> None:
    """Retry locally pending outcome notifications on the next bot activity."""
    for outcome in signal_lifecycle.pending_outcomes():
        send_signal_outcome(outcome, settings)


def send_alert(
    signal: TradeSignal,
    settings: Settings | None = None,
    *,
    source: str = "unknown",
    entry_bar_id: str | None = None,
    market_frame=None,
) -> dict[str, bool]:
    """Publish a new active idea once; duplicates stay silent until setup reset or exit."""
    settings = settings or get_settings()
    if not isinstance(signal, TradeSignal):
        # Retain the simple formatter/dispatcher contract for adapters and tests.
        return dispatch_message(format_message(signal), settings)

    if market_frame is None:
        logger.warning("refusing to publish %s/%s without its analyzed OHLCV frame", signal.symbol, signal.timeframe)
        return {"telegram": False, "discord": False, "desktop": False, "duplicate": False}

    from src.publish_gate import check_publish_allowed

    decision = check_publish_allowed(signal, settings)
    if not decision.allowed:
        blocked = DeliveryResult(telegram=False, discord=False, desktop=False, duplicate=False)
        blocked.blocked_reason = decision.reason or "رفضها فحص النشر"
        return blocked

    flush_pending_signal_outcomes(settings)
    tick_outcome = signal_lifecycle.observe_price(
        signal.symbol, signal.timeframe, signal.entry_price
    )
    if tick_outcome is not None:
        send_signal_outcome(tick_outcome, settings)

    registration = signal_lifecycle.register_signal(
        signal, source=source, entry_bar_id=entry_bar_id
    )
    if registration.closed_outcome is not None:
        send_signal_outcome(registration.closed_outcome, settings)
    if not registration.accepted:
        logger.info(
            "suppressing repeated %s signal for %s/%s while the setup is active or blocked",
            signal.direction.value, signal.symbol, signal.timeframe,
        )
        return {"telegram": False, "discord": False, "desktop": False, "duplicate": True}

    try:
        from src.charting import create_signal_chart

        chart_assets = create_signal_chart(signal, market_frame, registration.signal_id)
        if not signal_lifecycle.set_chart_assets(
            registration.signal_id, chart_assets.image_path, chart_assets.source_path
        ):
            raise RuntimeError("signal chart references could not be persisted")
    except Exception as exc:
        signal_lifecycle.cancel_signal(registration.signal_id)
        logger.error("refusing to publish signal without a valid chart (%s)", type(exc).__name__)
        return {"telegram": False, "discord": False, "desktop": False, "duplicate": False}

    message = format_message(signal, market_frame)
    _delivery_state.last_photo_message_id = None
    results = dispatch_signal(message, chart_assets.image_path, settings)
    message_id = getattr(_delivery_state, "last_photo_message_id", None)
    if message_id and results.get("telegram"):
        try:
            signal_lifecycle.set_telegram_message_id(registration.signal_id, message_id)
        except Exception as exc:
            logger.warning("could not store channel message id (%s)", type(exc).__name__)
    if not any(results.values()):
        signal_lifecycle.cancel_signal(registration.signal_id)
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
