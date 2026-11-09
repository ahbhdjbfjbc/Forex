"""On-demand Telegram interface — interactive Arabic menu, buttons, and instant scans.

Owns: the Telegram polling listener (commands + interactive buttons + free text)
and the scan-report message format. Calls Scanner 1 / Scanner 2's existing public
entry points for the actual regime/strategy math.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

try:
    from telegram import (
        BotCommand,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        ReplyKeyboardMarkup,
        Update,
    )
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    _TELEGRAM_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    BotCommand = None
    Update = object
    Application = None
    CallbackQueryHandler = None
    CommandHandler = None
    ContextTypes = None
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None
    KeyboardButton = None
    ReplyKeyboardMarkup = None
    MessageHandler = None
    filters = None
    _TELEGRAM_IMPORT_ERROR = exc

import strategy_evolver as evolver
from config import Settings, get_settings
from src.admin_audit import record_admin_action
from src import indicators as ind
from src import oanda_client
from src import publish_gate
from src.models import MarketRegime, RegimeResult
from src.notifier import (
    DIRECTION_EMOJI,
    DIRECTION_LABELS,
    REGIME_LABELS,
    STRATEGY_LABELS,
    dispatch_message,
    send_alert,
    send_signal_outcome,
)
from src.oanda_client import PaperTradingConfigError, PaperTradingRequestError
from src.scanner_1_regime import scan_regime_with_data, scan_symbols_with_data
from src.scanner_2_strategy import generate_signal
from src.signal_lifecycle import signal_lifecycle
from src.strategy_params import get_strategy_params
from src.symbol_resolver import display_pair, instrument_type, sanitize_symbol, to_yfinance_ticker
from src.watchlist_store import watchlist_store

logger = logging.getLogger(__name__)

DEVELOPER_CREDIT = "👨‍💻 المطور: Abodi sy"

# اختصارات عربية لتسهيل البحث السريع بدون كتابة رموز إنجليزية
_ARABIC_SYMBOL_ALIASES: dict[str, str] = {
    "ذهب": "XAUUSD",
    "الذهب": "XAUUSD",
    "فضة": "XAGUSD",
    "الفضة": "XAGUSD",
    "فضه": "XAGUSD",
    "بيتكوين": "BTCUSD",
    "البيتكوين": "BTCUSD",
    "بتكوين": "BTCUSD",
    "ايثيريوم": "ETHUSD",
    "إيثيريوم": "ETHUSD",
    "يورو": "EURUSD",
    "اليورو": "EURUSD",
    "يورو دولار": "EURUSD",
    "باوند": "GBPUSD",
    "الباوند": "GBPUSD",
    "استرليني": "GBPUSD",
    "باوند دولار": "GBPUSD",
    "ين": "USDJPY",
    "دولار ين": "USDJPY",
    "كندي": "USDCAD",
    "دولار كندي": "USDCAD",
    "استرالي": "AUDUSD",
    "أسترالي": "AUDUSD",
    "نيوزيلندي": "NZDUSD",
    "فرنك": "USDCHF",
    "دولار فرنك": "USDCHF",
    "مجنون": "GBPJPY",
    "المجنون": "GBPJPY",
    "باوند ين": "GBPJPY",
    "يورو ين": "EURJPY",
    "يورو باوند": "EURGBP",
}

# أشهر الأزواج السريعة للإضافة أو الفحص بضغطة زر
_POPULAR_SYMBOLS: list[tuple[str, str]] = [
    ("🥇 الذهب XAU/USD", "XAUUSD"),
    ("💶 اليورو EUR/USD", "EURUSD"),
    ("💷 الباوند GBP/USD", "GBPUSD"),
    ("💴 الين USD/JPY", "USDJPY"),
    ("🔥 المجنون GBP/JPY", "GBPJPY"),
    ("🍁 الكندي USD/CAD", "USDCAD"),
    ("🦘 الأسترالي AUD/USD", "AUDUSD"),
    ("🇨🇭 الفرنك USD/CHF", "USDCHF"),
    ("🥝 النيوزيلندي NZD/USD", "NZDUSD"),
    ("₿ البيتكوين BTC/USD", "BTCUSD"),
    ("💎 الإيثيريوم ETH/USD", "ETHUSD"),
    ("🥈 الفضة XAG/USD", "XAGUSD"),
]

# أزرار القائمة السفلية الثابتة لسهولة الاستخدام بدون كتابة
BTN_QUICK_SCAN = "🔍 فحص سريع للأزواج"
BTN_SCAN_ALL = "🚀 فحص شامل للسوق الآن"
BTN_WATCHLIST = "📋 قائمة المراقبة"
BTN_GOLD = "🥇 فحص الذهب (XAUUSD)"
BTN_BTC = "₿ فحص البيتكوين (BTCUSD)"
BTN_BOT_STATUS = "📊 حالة البوت والحساب"
BTN_EVOLVE_MENU = "🧪 تطوير الاستراتيجيات"
BTN_HELP = "📖 دليل الأوامر"
BTN_RESULTS = "📈 نتائج الصفقات"
RESULTS_PAGE_SIZE = 8

_NO_TRADE_REASONS = {
    MarketRegime.LOW_LIQUIDITY_CHOP: (
        "السوق في حالة تذبذب ضيق وضعف في السيولة. يُفضل الانتظار حتى حدوث اختراق واضح قبل الدخول."
    ),
    MarketRegime.RANGING_CONSOLIDATION: (
        "السعر يتحرك في مسار عرضي — لا يوجد حالياً تطابق بين أطراف البولنجر باند وتشبع مؤشر RSI."
    ),
    MarketRegime.STRONG_TREND_BULLISH: (
        "الاتجاه العام صاعد بقوة، لكن السعر لم يقم بعد بعملية تصحيح (ارتداد) نحو متوسط EMA 20 مع تأكيد RSI للدخول الآمن."
    ),
    MarketRegime.STRONG_TREND_BEARISH: (
        "الاتجاه العام هابط بقوة، لكن السعر لم يقم بعد بعملية تصحيح (ارتداد) نحو متوسط EMA 20 مع تأكيد RSI للدخول الآمن."
    ),
    MarketRegime.HIGH_VOLATILITY_EXPANSION: (
        "هناك توسع في التقلبات السعرية، لكن السعر لم يخترق قناة دونشيان (Donchian) بتأكيد سيولة كافية حتى الآن."
    ),
}

_USAGE = (
    "🤖 مَرحباً بك في بوت المسح والتوصيات الاحترافي\n"
    "يمكنك التحكم الكامل بالبوت عبر الأزرار التفاعلية بالأسفل دون الحاجة للكتابة، "
    "أو إرسال اسم الزوج مباشرة بالعربية أو الإنجليزية (مثل: ذهب، يورو، EURUSD، XAUUSD).\n\n"
    "📌 الأوامر المتاحة:\n"
    "• /menu — فتح لوحة التحكم والأزرار\n"
    "• /scan — فتح قائمة الأزرار لفحص أي زوج بضغطة زر\n"
    "• /scan_all — فحص شامل لجميع الأزواج في القائمة الآن\n"
    "• /watchlist — عرض وإدارة قائمة المراقبة التلقائية (حسب الفاصل المضبوط)\n"
    "• /add <الرمز> — إضافة زوج إلى قائمة الفحص التلقائي\n"
    "• /remove <الرمز> — حذف زوج من قائمة الفحص التلقائي\n"
    "• /results — نتائج كل الصفقات (ربح/خسارة/مفتوحة) مع ملخص الأداء\n"
    "• /trade <رقم> — تفاصيل صفقة واحدة مع صورة الشارت الأصلية\n"
    "• /id — معرفة رقم حسابك في تيليجرام (لضبط TELEGRAM_ALLOWED_USER_ID)\n"
    "• /status — عرض حالة البوت والإعدادات وفلتر الأخبار\n"
    "• /channel_test — اختبار إرسال رسالة إلى القناة\n"
    "• /paper_status — عرض رصيد الحساب التجريبي (OANDA) والصفقات المفتوحة\n"
    "• /evolve — اختبار وتطوير إعدادات الاستراتيجيات تاريخياً\n"
    "• /apply <رقم_المقترح> — اعتماد إعدادات استراتيجية ناجحة\n"
    "----------------------------------\n"
    f"{DEVELOPER_CREDIT}"
)

_TELEGRAM_COMMANDS = [
    ("start", "فتح لوحة التحكم"),
    ("menu", "فتح لوحة التحكم والأزرار"),
    ("scan", "فحص زوج أو فتح قائمة الأزواج"),
    ("scan_all", "فحص قائمة المراقبة كاملة"),
    ("watchlist", "عرض وإدارة قائمة المراقبة"),
    ("add", "إضافة زوج إلى قائمة المراقبة"),
    ("remove", "حذف زوج من قائمة المراقبة"),
    ("results", "نتائج كل الصفقات وملخص الأداء"),
    ("trade", "تفاصيل صفقة مع الشارت"),
    ("status", "عرض حالة البوت والإعدادات"),
    ("channel_test", "اختبار النشر في القناة وصلاحيات البوت"),
    ("id", "معرفة رقم حسابك في تيليجرام"),
    ("help", "عرض دليل الاستخدام"),
]


async def publish_telegram_command_menu(bot) -> None:
    """Publish Telegram's slash-command menu without blocking bot startup on API errors."""
    if BotCommand is None:
        return
    try:
        await bot.set_my_commands([BotCommand(name, description) for name, description in _TELEGRAM_COMMANDS])
    except Exception as exc:
        logger.warning("could not publish Telegram command menu (%s)", type(exc).__name__)


def _resolve_input_symbol(raw_text: str) -> str | None:
    """يحول الاسم العربي أو الإنجليزي إلى رمز زوج معتمد."""
    cleaned = raw_text.strip()
    if not cleaned:
        return None
    if cleaned in _ARABIC_SYMBOL_ALIASES:
        return sanitize_symbol(_ARABIC_SYMBOL_ALIASES[cleaned])
    return sanitize_symbol(cleaned)


def _build_main_reply_keyboard():
    """يبني لوحة الأزرار السفلية الثابتة لتسهيل الاستخدام بدون كتابة."""
    if ReplyKeyboardMarkup is None or KeyboardButton is None:
        return None
    keyboard = [
        [KeyboardButton(BTN_QUICK_SCAN), KeyboardButton(BTN_SCAN_ALL)],
        [KeyboardButton(BTN_GOLD), KeyboardButton(BTN_BTC)],
        [KeyboardButton(BTN_WATCHLIST), KeyboardButton(BTN_BOT_STATUS)],
        [KeyboardButton(BTN_RESULTS), KeyboardButton(BTN_EVOLVE_MENU), KeyboardButton(BTN_HELP)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


def _build_quick_scan_inline_keyboard():
    """يبني أزرار شفافة بجميع أزواج المراقبة وأشهر الأزواج للفحص بضغطة زر."""
    if InlineKeyboardMarkup is None or InlineKeyboardButton is None:
        return None

    watchlist_syms = watchlist_store.get_all()
    buttons = []
    row = []

    seen = set()
    for sym in watchlist_syms:
        clean_sym = sym.replace("=X", "")
        seen.add(clean_sym)
        row.append(InlineKeyboardButton(f"🔍 {display_pair(sym)}", callback_data=f"scan:{clean_sym}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
        row = []

    for label, sym in _POPULAR_SYMBOLS:
        if sym not in seen:
            row.append(InlineKeyboardButton(label, callback_data=f"scan:{sym}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton("🚀 فحص جميع أزواج القائمة الآن", callback_data="action:scan_all")])
    return InlineKeyboardMarkup(buttons)


def _build_report_action_keyboard(symbol: str, has_signal: bool = False):
    """أزرار سريعة أسفل تقرير الفحص لإعادة الفحص أو النشر في القناة أو اختيار زوج آخر."""
    if InlineKeyboardMarkup is None or InlineKeyboardButton is None:
        return None
    clean_sym = symbol.replace("=X", "")
    rows = [
        [
            InlineKeyboardButton("🔄 إعادة فحص الزوج", callback_data=f"scan:{clean_sym}"),
            InlineKeyboardButton("📋 فحص زوج آخر", callback_data="action:open_scan_menu"),
        ]
    ]
    if has_signal:
        rows.insert(
            0,
            [InlineKeyboardButton("📤 إرسال هذه التوصية للقناة الآن", callback_data=f"pub:{clean_sym}")],
        )
    return InlineKeyboardMarkup(rows)


def _build_watchlist_inline_keyboard():
    """أزرار تفاعلية لإدارة قائمة المراقبة (فحص أو حذف أو إضافة أزواج بضغطة زر)."""
    if InlineKeyboardMarkup is None or InlineKeyboardButton is None:
        return None
    symbols = watchlist_store.get_all()
    rows = []
    for sym in symbols:
        clean_sym = sym.replace("=X", "")
        rows.append(
            [
                InlineKeyboardButton(f"🔍 فحص {display_pair(sym)}", callback_data=f"scan:{clean_sym}"),
                InlineKeyboardButton(f"🗑️ حذف {display_pair(sym)}", callback_data=f"rem:{clean_sym}"),
            ]
        )
    rows.append([InlineKeyboardButton("➕ إضافة أزواج شهيرة بضغطة زر", callback_data="action:add_menu")])
    rows.append([InlineKeyboardButton("🚀 فحص كل القائمة الآن", callback_data="action:scan_all")])
    return InlineKeyboardMarkup(rows)


def _build_add_symbols_inline_keyboard():
    """قائمة أزرار لإضافة الأزواج الشهيرة إلى قائمة المراقبة بضغطة زر."""
    if InlineKeyboardMarkup is None or InlineKeyboardButton is None:
        return None
    current = {s.replace("=X", "") for s in watchlist_store.get_all()}
    rows = []
    row = []
    for label, sym in _POPULAR_SYMBOLS:
        status = "✅ " if sym in current else "➕ "
        row.append(InlineKeyboardButton(f"{status}{label}", callback_data=f"add:{sym}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 العودة لقائمة المراقبة", callback_data="action:watchlist")])
    return InlineKeyboardMarkup(rows)


def _format_found_report(
    symbol: str, primary_tf: str, scanned_tfs: list[str], regime_result: RegimeResult, signal, df
) -> str:
    kind = instrument_type(symbol)
    rsi_val = float(ind.rsi(df["Close"], 14).iloc[-1])
    direction_emoji = DIRECTION_EMOJI.get(signal.direction, "")
    direction_label = DIRECTION_LABELS.get(signal.direction, signal.direction.value)
    regime_label = REGIME_LABELS.get(regime_result.regime, regime_result.regime.value)
    strategy_label = STRATEGY_LABELS.get(signal.strategy, signal.strategy.value)
    tf_tag = "/".join(tf.upper() for tf in scanned_tfs) or primary_tf.upper()
    confidence_pct = int(round(regime_result.confidence * 100))

    if kind == "FX":
        sl_line = f"🛑 وقف الخسارة: {signal.stop_loss_price} (-{signal.stop_loss_pips:g} نقطة)"
        target_line = (
            f"🎯 الهدف الوحيد: {signal.take_profit_2_price} "
            f"(+{signal.take_profit_2_pips:g} نقطة | العائد 1:{signal.take_profit_2_rr:g})"
        )
        size_line = (
            f"📦 اللوت المقترح: {signal.risk.lot_size:g} لوت "
            f"(مخاطرة {signal.risk.risk_percent:g}% من رصيد ${signal.risk.account_size_usd:,.0f})"
        )
    else:
        unit_label = (
            "بيتكوين"
            if symbol.startswith("BTC")
            else "إيثيريوم"
            if symbol.startswith("ETH")
            else "أونصة"
            if kind == "METAL"
            else "وحدة"
        )
        sl_line = f"🛑 وقف الخسارة: {signal.stop_loss_price} (-{signal.stop_loss_pips:g})"
        target_line = (
            f"🎯 الهدف الوحيد: {signal.take_profit_2_price} "
            f"(+{signal.take_profit_2_pips:g} | العائد 1:{signal.take_profit_2_rr:g})"
        )
        size_line = (
            f"📦 الحجم المقترح: {signal.risk.lot_size:g} {unit_label} "
            f"(مخاطرة {signal.risk.risk_percent:g}% من رصيد ${signal.risk.account_size_usd:,.0f})"
        )

    return "\n".join(
        [
            f"📊 تقرير الفحص الفوري: {display_pair(symbol)}",
            "----------------------------------",
            f"🔍 حالة السوق: {regime_label} ({tf_tag})",
            f"⚡ الاستراتيجية المطابقة: {strategy_label}",
            f"📐 مطابقة قواعد النظام: {confidence_pct}% — ليست نسبة نجاح تاريخية",
            *(
                [f"🧮 تقييم شروط الفرصة: {signal.quality_score}/100 ({signal.quality_grade}) — ليس نسبة نجاح"]
                if signal.quality_score is not None and signal.quality_grade
                else []
            ),
            f"🎯 التوصية: دخول صفقة {direction_label} {direction_emoji}",
            "",
            f"📈 سعر الدخول: {signal.entry_price}",
            sl_line,
            target_line,
            *(
                [f"⚠️ مصدر الذهب: GC=F (عقود COMEX الآجلة)، وليس سعراً فورياً XAUUSD"]
                if symbol.upper() == "XAUUSD"
                else []
            ),
            size_line,
            "----------------------------------",
            f"💡 المؤشرات الفنية: ADX = {regime_result.metrics.adx:.1f} | "
            f"ATR = {regime_result.metrics.atr:.5f} | RSI = {rsi_val:.0f}",
            f"{DEVELOPER_CREDIT}",
        ]
    )


def _format_no_trade_report(
    symbol: str, primary_tf: str, scanned_tfs: list[str], regime_result: RegimeResult
) -> str:
    tf_tag = "/".join(tf.upper() for tf in scanned_tfs) or primary_tf.upper()
    regime_label = REGIME_LABELS.get(regime_result.regime, regime_result.regime.value)
    reason = _NO_TRADE_REASONS.get(
        regime_result.regime, "لا توجد إشارة دخول مكتملة الشروط تتطابق مع وضع السوق الحالي."
    )
    return "\n".join(
        [
            f"📊 تقرير الفحص الفوري: {display_pair(symbol)}",
            "----------------------------------",
            f"🔍 حالة السوق: {regime_label} ({tf_tag})",
            "⚡ الاستراتيجية المطابقة: لا يوجد",
            "⚠️ التوصية: عدم التداول حالياً (انتظار) 🔴",
            "",
            f"💡 السبب الفني: {reason}",
            "----------------------------------",
            f"{DEVELOPER_CREDIT}",
        ]
    )


def run_on_demand_scan(raw_text: str, settings: Settings | None = None) -> str:
    """Sanitize free-text input, scan it, and return a formatted Arabic report."""
    settings = settings or get_settings()

    symbol = _resolve_input_symbol(raw_text)
    if symbol is None:
        return _USAGE

    fetch_ticker = to_yfinance_ticker(symbol)
    primary_tf = settings.telegram_primary_timeframe
    candidate_tfs = list(dict.fromkeys([primary_tf, *settings.timeframe_list]))

    scanned: dict[str, tuple[RegimeResult, object]] = {}
    for tf in candidate_tfs:
        outcome = scan_regime_with_data(symbol, tf, settings, fetch_symbol=fetch_ticker)
        if outcome is not None:
            scanned[tf] = outcome

    if not scanned:
        return (
            f"📊 تقرير الفحص الفوري: {display_pair(symbol)}\n"
            "----------------------------------\n"
            f"⚠️ تعذر جلب بيانات السوق للرمز {symbol} ({fetch_ticker}). "
            "تأكد من صحة رمز الزوج أو حاول مرة أخرى بعد قليل."
        )

    watchlist_store.add(symbol)

    effective_primary_tf = primary_tf if primary_tf in scanned else next(iter(scanned))
    regime_result, df = scanned[effective_primary_tf]
    scanned_tfs = list(scanned.keys())

    signal = generate_signal(regime_result, df, settings)
    if signal is not None:
        decision = publish_gate.check_publish_allowed(signal, settings)
        if not decision.allowed:
            return (
                f"📊 تقرير الفحص: {display_pair(symbol)}\n"
                "⛔ لم يصدر النظام توصية قابلة للنشر، ولم تُرسل إلى أي محادثة.\n"
                f"السبب: {decision.reason}"
            )
        return _format_found_report(symbol, effective_primary_tf, scanned_tfs, regime_result, signal, df)
    return _format_no_trade_report(symbol, effective_primary_tf, scanned_tfs, regime_result)


def _hide_trade_details_in_command_reply(report: str) -> str:
    """Keep command chats responsive while reserving full signals for the signal channel."""
    if "🎯 التوصية: دخول صفقة" in report:
        return (
            "✅ اكتمل التحليل ووجد النظام مرشحاً أولياً؛ هذا ليس إثباتاً لربحية أو تنفيذ صفقة.\n"
            "🔒 لا تُعرض تفاصيل الدخول والوقف والهدف في محادثة الأوامر؛ "
            "ولا يُنشر شيء ما لم يكن السوق مفتوحاً ومصدر الأسعار اللحظي موثقاً. "
            "التوصيات المقبولة تُرسل إلى قناة الإشارات المحددة فقط."
        )
    if "🎯 التوصيات والفرص المكتملة:" in report:
        return (
            "✅ اكتمل الفحص الشامل. تفاصيل التوصيات لا تُكرر في محادثة الأوامر؛ "
            "ويُحجب النشر إذا كان السوق مغلقاً أو مصدر الأسعار اللحظي غير موثق. "
            "التوصية المقبولة تُرسل بصورة إلى قناة الإشارات المحددة فقط.\n"
            "📈 أرسل /results لمتابعة النتائج المسجلة."
        )
    return report


async def _run_scan_in_worker(scan_function, *args, operation: str) -> str:
    """Run market work off-loop and convert unexpected failures into a visible reply."""
    try:
        return await asyncio.to_thread(scan_function, *args)
    except Exception as exc:
        logger.exception("Telegram %s failed; returning a user-facing scan error", operation)
        return (
            "⚠️ تعذر إكمال الفحص بسبب خطأ مؤقت في جلب البيانات أو تحليلها "
            f"({type(exc).__name__}). تحقق من /status و/results، ثم حاول لاحقاً."
        )


def run_full_watchlist_scan(settings: Settings | None = None) -> str:
    """يفحص جميع أزواج قائمة المراقبة دفعة واحدة ويرجع تقريراً شاملاً بالعربية."""
    settings = settings or get_settings()
    symbols = watchlist_store.get_all()
    if not symbols:
        return "⚠️ قائمة المراقبة فارغة حالياً. أضف أزواجاً عبر زر قائمة المراقبة أولاً."

    regime_data = scan_symbols_with_data(symbols, settings)
    if not regime_data:
        return "⚠️ تعذر جلب البيانات في الوقت الحالي. يرجى المحاولة بعد قليل."

    found_signals = []
    ranging_or_waiting = []

    for regime_result, df in regime_data:
        pair_str = f"{display_pair(regime_result.symbol)} ({regime_result.timeframe.upper()})"
        try:
            outcome = signal_lifecycle.observe_frame(
                regime_result.symbol, regime_result.timeframe, df
            )
            if outcome is not None:
                send_signal_outcome(outcome, settings)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            logger.exception(
                "could not evaluate active recommendation outcome for %s/%s",
                regime_result.symbol, regime_result.timeframe,
            )

        signal = generate_signal(regime_result, df, settings)
        if signal is not None and regime_result.confidence >= settings.min_signal_confidence:
            dir_emoji = DIRECTION_EMOJI.get(signal.direction, "")
            dir_label = DIRECTION_LABELS.get(signal.direction, signal.direction.value)
            conf_pct = int(round(regime_result.confidence * 100))
            delivery = send_alert(
                signal, settings, source="telegram-scan-all", entry_bar_id=str(df.index[-1]),
                market_frame=df,
            )
            if getattr(delivery, "blocked_reason", None):
                state = f"لم تُنشر: {delivery.blocked_reason}"
            elif delivery.get("duplicate"):
                state = "لديها توصية نشطة بالفعل؛ لم تُكرر الرسالة"
            elif any(value for channel, value in delivery.items() if channel != "duplicate"):
                state = "تم إرسالها"
            else:
                state = "تعذر إرسالها؛ تحقق من إعدادات قنوات الإشعار"
            found_signals.append(
                f"✅ {pair_str}: {dir_label} {dir_emoji} (مطابقة القواعد: {conf_pct}%، تقييم الشروط: "
                f"{signal.quality_score}/100) — {state}"
            )
        else:
            signal_lifecycle.reset_block(regime_result.symbol, regime_result.timeframe)
            reg_label = REGIME_LABELS.get(regime_result.regime, regime_result.regime.value)
            ranging_or_waiting.append(f"⏳ {pair_str}: {reg_label}")

    lines = [
        "🚀 تقرير الفحص الشامل للسوق 🚀",
        f"🔍 تم فحص ({len(regime_data)}) من الأزواج والأطر الزمنية:",
        "----------------------------------",
    ]
    if found_signals:
        lines.append("🎯 التوصيات والفرص المكتملة:")
        lines.extend(found_signals)
        lines.append("----------------------------------")
    else:
        lines.append("⚠️ لا توجد فرص مكتملة الشروط عالية الدقة في هذه اللحظة.")
        lines.append("----------------------------------")

    lines.append("📊 حالة باقي الأزواج (انتظار / تذبذب):")
    lines.extend(ranging_or_waiting[:15])
    if len(ranging_or_waiting) > 15:
        lines.append(f"... و ({len(ranging_or_waiting) - 15}) من الأطر الزمنية الأخرى في حالة انتظار.")
    lines.append("----------------------------------")
    lines.append(DEVELOPER_CREDIT)

    return "\n".join(lines)


def _is_authorized(update: Update, settings: Settings) -> bool:
    """Fail closed: every interactive/admin action requires the numeric owner ID."""
    allowed_user_id = str(getattr(settings, "telegram_allowed_user_id", "") or "").strip()
    user = update.effective_user
    chat = update.effective_chat
    if not allowed_user_id.isdecimal() or user is None or chat is None:
        return False
    if str(user.id) != allowed_user_id:
        return False
    chat_type = getattr(chat, "type", "private")
    if chat_type == "private":
        return True
    if chat_type in {"group", "supergroup"}:
        configured_chat = str(settings.telegram_chat_id or "").strip()
        return bool(configured_chat and str(chat.id) == configured_chat)
    return False


def _denial_hint(update: Update) -> str:
    """Tell the sender their own numeric ID so the owner can configure it correctly."""
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    user_id = getattr(user, "id", None)
    if user_id is None:
        return ""
    hint = f"\n\n🆔 رقم حسابك: {user_id}"
    chat_type = getattr(chat, "type", "private")
    if chat_type in {"group", "supergroup"}:
        hint += f"\n💬 رقم هذه المجموعة: {getattr(chat, 'id', '')} (يجب أن يطابق TELEGRAM_CHAT_ID)"
    hint += "\nإذا كنت المالك، ضع هذا الرقم في TELEGRAM_ALLOWED_USER_ID ثم أعد تشغيل الخدمة."
    return hint


async def _reply_target(update: Update):
    """يرجع الكائن المناسب للرد سواء كانت ضغطة زر أو رسالة عادية."""
    if getattr(update, "message", None) is not None:
        return update.message
    query = getattr(update, "callback_query", None)
    if query is not None and getattr(query, "message", None) is not None:
        return query.message
    return None


async def _require_authorized(
    update: Update, settings: Settings, msg_target=None
) -> bool:
    """Authorize a command and explain denial instead of silently ignoring it."""
    user = getattr(update, "effective_user", None)
    query = getattr(update, "callback_query", None)
    message = getattr(update, "message", None)
    raw_action = getattr(message, "text", "") if message is not None else ""
    if query is not None:
        raw_action = "callback:" + str(getattr(query, "data", "")).split(":", 1)[0]
    elif isinstance(raw_action, str) and raw_action.startswith("/"):
        raw_action = raw_action.split(maxsplit=1)[0].split("@", 1)[0]
    else:
        raw_action = "text"
    if _is_authorized(update, settings):
        record_admin_action(getattr(user, "id", None), raw_action, "authorized")
        return True
    record_admin_action(getattr(user, "id", None), raw_action, "denied")
    logger.warning("denying Telegram command from unauthorized sender")
    target = msg_target or await _reply_target(update)
    if target is not None:
        await target.reply_text(
            "⛔ وصل الأمر إلى البوت لكن هذا الحساب غير مخوّل. "
            "يلزم ضبط TELEGRAM_ALLOWED_USER_ID على رقم حساب المالك المصرّح به."
            + _denial_hint(update)
        )
    return False


async def _handle_scan_request(update: Update, settings: Settings, raw_text: str) -> None:
    if not await _require_authorized(update, settings):
        return
    msg_target = await _reply_target(update)
    if not msg_target:
        return

    symbol = _resolve_input_symbol(raw_text)
    wait_msg = None
    if symbol:
        wait_msg = await msg_target.reply_text(
            f"⏳ جاري فحص {display_pair(symbol)} وتحليل المؤشرات الفنية بدقة..."
        )

    report = await _run_scan_in_worker(
        run_on_demand_scan, raw_text, settings, operation="on-demand scan"
    )
    has_signal = "🎯 التوصية: دخول صفقة" in report
    reply_markup = (
        _build_report_action_keyboard(symbol, has_signal)
        if symbol
        else _build_quick_scan_inline_keyboard()
    )

    if wait_msg:
        try:
            await wait_msg.delete()
        except Exception:
            pass

    await msg_target.reply_text(_hide_trade_details_in_command_reply(report), reply_markup=reply_markup)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not _is_authorized(update, settings):
        record_admin_action(
            getattr(getattr(update, "effective_user", None), "id", None), "/start", "denied"
        )
        logger.warning("denying /start from an unauthorized Telegram sender")
        if msg_target:
            await msg_target.reply_text(
                "⛔ لا تملك صلاحية استخدام لوحة البوت. يلزم ضبط "
                "TELEGRAM_ALLOWED_USER_ID على رقم حساب المالك المصرّح به."
                + _denial_hint(update)
            )
        return
    record_admin_action(
        getattr(getattr(update, "effective_user", None), "id", None), "/start", "authorized"
    )
    if msg_target:
        await msg_target.reply_text(
            f"✅ بوت توصيات الفوركس الاحترافي متصل ويعمل بنجاح!\n\n{_USAGE}",
            reply_markup=_build_main_reply_keyboard(),
        )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    raw_text = " ".join(context.args) if getattr(context, "args", None) else ""
    if not raw_text:
        await msg_target.reply_text(
            "🔍 اختر الزوج الذي تريد فحصه الآن بضغطة زر:\n"
            "(أو اكتب اسم الزوج مباشرة مثل: ذهب أو EURUSD)",
            reply_markup=_build_quick_scan_inline_keyboard(),
        )
        return
    await _handle_scan_request(update, settings, raw_text)


async def cmd_scan_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    wait_msg = await msg_target.reply_text(
        "🚀 جاري إجراء فحص شامل لجميع الأزواج في قائمة المراقبة... يرجى الانتظار لحظات."
    )
    report = await _run_scan_in_worker(
        run_full_watchlist_scan, settings, operation="full watchlist scan"
    )
    try:
        await wait_msg.delete()
    except Exception:
        pass
    await msg_target.reply_text(
        _hide_trade_details_in_command_reply(report),
        reply_markup=_build_quick_scan_inline_keyboard(),
    )


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    symbols = watchlist_store.get_all()
    if not symbols:
        await msg_target.reply_text(
            "⚠️ قائمة الفحص التلقائي فارغة حالياً. اضغط على الزر بالأسفل لإضافة الأزواج الشهيرة فوراً:",
            reply_markup=_build_add_symbols_inline_keyboard(),
        )
        return
    lines = [
        f"📋 قائمة المراقبة التلقائية ({len(symbols)} أزواج — تُفحص كل {settings.auto_scan_interval_label_ar}):",
        "اضغط على أي زوج لفحصه فوراً أو حذفه من القائمة:",
    ]
    await msg_target.reply_text(
        "\n".join(lines),
        reply_markup=_build_watchlist_inline_keyboard(),
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    raw = " ".join(context.args) if getattr(context, "args", None) else ""
    if not raw:
        await msg_target.reply_text(
            "➕ اختر الأزواج التي تريد إضافتها لقائمة المراقبة بضغطة زر:",
            reply_markup=_build_add_symbols_inline_keyboard(),
        )
        return
    resolved = _resolve_input_symbol(raw) or raw
    symbol, was_new = watchlist_store.add(resolved)
    record_admin_action(
        getattr(getattr(update, "effective_user", None), "id", None),
        "/add", "failed" if symbol is None else "succeeded",
    )
    if symbol is None:
        await msg_target.reply_text(
            f"⚠️ الرمز '{raw}' غير صالح.\nاختر من القائمة التالية:",
            reply_markup=_build_add_symbols_inline_keyboard(),
        )
    elif was_new:
        await msg_target.reply_text(
            f"✅ تمت إضافة {display_pair(symbol)} إلى قائمة الفحص الدوري (كل {settings.auto_scan_interval_label_ar}).",
            reply_markup=_build_watchlist_inline_keyboard(),
        )
    else:
        await msg_target.reply_text(
            f"ℹ️ الزوج {display_pair(symbol)} موجود بالفعل في قائمة المراقبة.",
            reply_markup=_build_watchlist_inline_keyboard(),
        )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    raw = " ".join(context.args) if getattr(context, "args", None) else ""
    if not raw:
        await msg_target.reply_text(
            "🗑️ لحذف زوج، اضغط على زر 🗑️ حذف بجانب اسم الزوج في القائمة:",
            reply_markup=_build_watchlist_inline_keyboard(),
        )
        return
    resolved = _resolve_input_symbol(raw) or raw
    symbol, was_removed = watchlist_store.remove(resolved)
    record_admin_action(
        getattr(getattr(update, "effective_user", None), "id", None),
        "/remove", "failed" if symbol is None else "succeeded",
    )
    if symbol is None:
        await msg_target.reply_text(f"⚠️ الرمز '{raw}' غير صالح.")
    elif was_removed:
        await msg_target.reply_text(
            f"🗑️ تم حذف {display_pair(symbol)} من قائمة الفحص التلقائي.",
            reply_markup=_build_watchlist_inline_keyboard(),
        )
    else:
        await msg_target.reply_text(f"ℹ️ الزوج {display_pair(symbol)} غير موجود في القائمة.")


def _format_paper_status(settings: Settings) -> str:
    if not settings.enable_paper_trading:
        return "ℹ️ التداول التجريبي غير مفعل حالياً (ENABLE_PAPER_TRADING=false)."
    try:
        summary = oanda_client.get_account_summary(settings)
        trades = oanda_client.get_open_trades(settings)
    except (PaperTradingConfigError, PaperTradingRequestError) as exc:
        return f"⚠️ تعذر الاتصال بحساب OANDA التجريبي: {exc}"

    account = summary.get("account", {})
    lines = [
        "📄 حالة الحساب التجريبي (OANDA Practice):",
        f"💰 الرصيد الحالي: {account.get('balance')} {account.get('currency', '')}",
        f"📈 الربح/الخسارة العائمة: {account.get('unrealizedPL')}",
        f"📂 الصفقات المفتوحة: {len(trades)}",
    ]
    for t in trades[:10]:
        lines.append(f"  • {t.get('instrument')} {t.get('currentUnits')} @ {t.get('price')}")
    return "\n".join(lines)


async def cmd_paper_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    report = await asyncio.to_thread(_format_paper_status, settings)
    await msg_target.reply_text(report)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض حالة البوت والإعدادات الحالية وحالة الحساب التجريبي."""
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    symbols = watchlist_store.get_all()
    channel_status = (
        f"مربوطة ({settings.telegram_channel_id})"
        if getattr(settings, "telegram_channel_id", "").strip()
        else "يرسل للمحادثة/القناة الافتراضية"
    )
    news_status = "مفعل ✅" if getattr(settings, "enable_news_filter", True) else "معطل ❌"
    no_sig_status = "مفعل ✅" if getattr(settings, "notify_no_signals", False) else "معطل ❌"
    conf_pct = int(round(settings.min_signal_confidence * 100))

    lines = [
        "📊 تقرير حالة البوت والإعدادات:",
        "----------------------------------",
        f"⏱️ دورة الفحص التلقائي: كل {settings.auto_scan_interval_label_ar}",
        f"🎯 الحد الأدنى لدقة التوصية: {conf_pct}%",
        f"📋 عدد الأزواج المراقبة: {len(symbols)} أزواج",
        f"🕒 الأطر الزمنية للفحص: {', '.join(settings.timeframe_list).upper()}",
        f"📢 قناة نشر التوصيات: {channel_status}",
        f"📰 فلتر التوقف وقت الأخبار: {news_status}",
        f"⚠️ تنبيه التذبذب وعدم وجود فرص: {no_sig_status}",
        f"💼 إدارة المخاطر: {settings.risk_percent_per_trade:g}% من رصيد ${settings.account_size_usd:,.0f}",
        "----------------------------------",
    ]
    lines.append(await check_channel_permissions(getattr(context, "bot", None), settings))
    try:
        summary = await asyncio.to_thread(signal_lifecycle.performance_summary)
        lines.append(_format_summary_line(summary))
    except Exception as exc:
        logger.warning("results summary unavailable for /status (%s)", type(exc).__name__)
    try:
        from src.tradingview_bridge import last_tradingview_alert_label

        lines.append(f"📡 آخر تنبيه TradingView: {last_tradingview_alert_label()}")
    except Exception:
        pass
    lines.append("----------------------------------")
    paper_text = await asyncio.to_thread(_format_paper_status, settings)
    lines.append(paper_text)
    lines.append("----------------------------------")
    lines.append(DEVELOPER_CREDIT)
    await msg_target.reply_text("\n".join(lines), reply_markup=_build_main_reply_keyboard())


async def cmd_channel_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل رسالة اختبارية للقناة للتأكد من صلاحيات البوت."""
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    test_msg = (
        "✅ رسالة اختبار من بوت توصيات الفوركس\n"
        "هذه قناة نشر الإشارات ونتائجها؛ تنبيهات الأعطال والأخبار تصل للأدمن على الخاص.\n"
        "------------------------------\n"
        f"{DEVELOPER_CREDIT}"
    )
    permission_report = await check_channel_permissions(context.bot, settings)
    await msg_target.reply_text(permission_report)
    res = await asyncio.to_thread(
        dispatch_message,
        test_msg,
        settings,
        target_chat_id=str(getattr(settings, "telegram_channel_id", "") or "").strip(),
    )
    if res.get("telegram"):
        await msg_target.reply_text("✅ تم إرسال رسالة الاختبار إلى القناة بنجاح!")
    else:
        await msg_target.reply_text(
            "❌ تعذر الإرسال إلى القناة. تأكد من:\n"
            "1. إضافة البوت كمشرف (Admin) في القناة مع صلاحية نشر الرسائل.\n"
            "2. كتابة معرف القناة بشكل صحيح في TELEGRAM_CHANNEL_ID داخل ملف .env."
        )


def _format_evolution_report(
    strategy_name: str, symbol: str, timeframe: str, candidate, tested_count: int
) -> str:
    r = candidate.result
    oos = candidate.out_of_sample_result
    baseline_oos = candidate.baseline_out_of_sample_result
    baseline = asdict(get_strategy_params(strategy_name))
    change_lines = [f"{key}: {baseline.get(key)} -> {new_val}" for key, new_val in candidate.param_overrides.items()]
    changes_text = "; ".join(change_lines) if change_lines else "لا توجد تغييرات عن الإعدادات الافتراضية"

    pf = r.profit_factor
    pf_display = f"{pf:.2f}" if isinstance(pf, float) and pf != float("inf") else "∞"

    if candidate.passed:
        status_line = (
            f"✅ النتيجة: اجتاز التدريب والاختبار خارج العينة ({oos.total_trades} صفقة OOS) "
            f"وتفوق على خط الأساس ({baseline_oos.avg_realized_r:+g}R متوسطاً).\n"
            f"لتطبيق هذه الإعدادات أرسل الأمر: /apply {candidate.proposal_id}"
        )
    else:
        status_line = (
            f"❌ النتيجة: لم يجتز حواجز التدريب/OOS (يلزم {evolver.MIN_OUT_OF_SAMPLE_TRADES} صفقة OOS، "
            "المعايير الإحصائية، وتفوق متوسط R على خط الأساس) — لم يتم التطبيق."
        )

    return "\n".join(
        [
            "🧪 تقرير تطوير واختبار الاستراتيجية (Backtest)",
            "----------------------------------",
            f"🧠 الاستراتيجية: {evolver.STRATEGY_DISPLAY_LABELS.get(strategy_name, strategy_name)}",
            f"📌 الزوج: {display_pair(symbol)} ({timeframe.upper()})",
            "",
            f"📚 مقاييس التدريب للمرشح المختار من ({tested_count}) إعداد (من {r.start.date()} إلى {r.end.date()}):",
            f"• Win Rate: {r.win_rate_pct:.1f}% | PF: {pf_display} | Sharpe: {r.sharpe_ratio:.2f}",
            f"• التراجع الأقصى: -{r.max_drawdown_pct:.1f}% | الصفقات: {r.total_trades}",
            f"🧪 الاختبار خارج العينة ({oos.start.date()} إلى {oos.end.date()}): "
            f"{oos.total_trades} صفقة، متوسط {oos.avg_realized_r:+g}R، Sharpe {oos.sharpe_ratio:.2f}",
            f"🔎 خط الأساس الحالي في OOS: {baseline_oos.total_trades} صفقة، "
            f"متوسط {baseline_oos.avg_realized_r:+g}R — ترتيب المرشحين يعتمد على التدريب فقط.",
            "",
            status_line,
            "----------------------------------",
            f"💡 التعديلات المختبرة: {changes_text}",
            f"🆔 رقم المقترح: {candidate.proposal_id}",
            f"{DEVELOPER_CREDIT}",
        ]
    )


def run_evolution(strategy_name: str, symbol: str, timeframe: str, settings: Settings) -> str:
    grid = evolver.DEFAULT_GRIDS.get(strategy_name, {})
    try:
        candidates = evolver.evolve_strategy(strategy_name, symbol, timeframe, grid, settings, lookback_months=6)
    except Exception:
        logger.exception("evolution run failed for %s/%s/%s", strategy_name, symbol, timeframe)
        return f"⚠️ فشل تشغيل اختبار الاستراتيجية لـ {display_pair(symbol)} ({timeframe.upper()}) — راجع السجلات."

    if not candidates:
        return (
            f"🧪 تقرير تطوير الاستراتيجية\n----------------------------------\n"
            f"الاستراتيجية: {evolver.STRATEGY_DISPLAY_LABELS.get(strategy_name, strategy_name)}\n"
            f"الزوج: {display_pair(symbol)} ({timeframe.upper()})\n\n"
            "⚠️ لا توجد نتائج اختبار كافية — البيانات التاريخية غير كافية لهذا الزوج/الإطار الزمني."
        )

    best = candidates[0]
    return _format_evolution_report(strategy_name, symbol, timeframe, best, len(candidates))


async def cmd_evolve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return

    args = getattr(context, "args", None) or []
    if len(args) < 2:
        buttons = []
        if InlineKeyboardMarkup and InlineKeyboardButton:
            buttons = [
                [InlineKeyboardButton("🔄 ارتداد الاتجاه (EURUSD 1H)", callback_data="evol:trend_pullback:EURUSD:1h")],
                [InlineKeyboardButton("🥇 ارتداد الاتجاه (XAUUSD 1H)", callback_data="evol:trend_pullback:XAUUSD:1h")],
                [InlineKeyboardButton("📊 الارتداد للوسط (EURUSD 1H)", callback_data="evol:mean_reversion:EURUSD:1h")],
                [InlineKeyboardButton("💥 الاختراق السعري (GBPUSD 1H)", callback_data="evol:breakout:GBPUSD:1h")],
            ]
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        await msg_target.reply_text(
            "🧪 تطوير واختبار الاستراتيجيات (Backtest):\n"
            "اضغط على أحد الاختبارات الجاهزة بالأسفل، أو استخدم الصيغة:\n"
            "/evolve <الاستراتيجية> <الزوج> [الإطار_الزمني]\n\n"
            f"الاستراتيجيات المتاحة: {', '.join(sorted(evolver.PARAM_CLASSES))}\n"
            "مثال: /evolve trend_pullback EURUSD 1h",
            reply_markup=markup,
        )
        return

    strategy_name = args[0].lower()
    if strategy_name not in evolver.PARAM_CLASSES:
        await msg_target.reply_text(
            f"⚠️ استراتيجية غير معروفة '{args[0]}'. الخيارات المتاحة: {', '.join(sorted(evolver.PARAM_CLASSES))}"
        )
        return

    symbol = _resolve_input_symbol(args[1])
    if symbol is None:
        await msg_target.reply_text(f"⚠️ الرمز '{args[1]}' غير صالح.")
        return

    timeframe = args[2].lower() if len(args) > 2 else settings.telegram_primary_timeframe

    grid = evolver.DEFAULT_GRIDS.get(strategy_name, {})
    combo_count = 1
    for values in grid.values():
        combo_count *= len(values)

    await msg_target.reply_text(
        f"🧪 جاري اختبار وتطوير استراتيجية {evolver.STRATEGY_DISPLAY_LABELS.get(strategy_name, strategy_name)} "
        f"على زوج {display_pair(symbol)} ({timeframe.upper()}) عبر تجربة ({combo_count}) إعدادات مختلفة لآخر 6 أشهر...\n"
        "⏳ قد يستغرق هذا الفحص دقيقة إلى دقيقتين..."
    )

    report = await asyncio.to_thread(run_evolution, strategy_name, symbol, timeframe, settings)
    await msg_target.reply_text(report)


async def cmd_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return

    args = getattr(context, "args", None) or []
    if not args:
        await msg_target.reply_text("ℹ️ طريقة الاستخدام: /apply <رقم_المقترح> (يؤخذ من تقرير /evolve).")
        return

    proposal_id = args[0]
    proposal = evolver.load_proposal(proposal_id)
    if proposal is None:
        await msg_target.reply_text(f"⚠️ لم يتم العثور على مقترح بالرقم {proposal_id}.")
        return

    applied = await asyncio.to_thread(evolver.apply_proposal, proposal_id)
    record_admin_action(
        getattr(getattr(update, "effective_user", None), "id", None),
        "/apply", "succeeded" if applied else "failed",
    )
    if applied:
        await msg_target.reply_text(
            f"✅ تم تطبيق المقترح {proposal_id} بنجاح على استراتيجية "
            f"{evolver.STRATEGY_DISPLAY_LABELS.get(proposal['strategy_name'], proposal['strategy_name'])}.\n"
            "ستعمل التوصيات القادمة بالإعدادات الجديدة تلقائياً، وتم حفظ نسخة احتياطية من الإعدادات السابقة."
        )
    else:
        await msg_target.reply_text(
            f"❌ تعذر تطبيق المقترح {proposal_id} لأنه لم يجتز معايير القبول والأمان."
        )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج جميع ضغطات الأزرار التفاعلية (Inline Buttons) بسرعة وسهولة."""
    settings = get_settings()
    query = getattr(update, "callback_query", None)
    if query is None:
        return

    if not _is_authorized(update, settings):
        record_admin_action(
            getattr(getattr(update, "effective_user", None), "id", None),
            "callback:" + str(getattr(query, "data", "")).split(":", 1)[0],
            "denied",
        )
        await query.answer("⚠️ غير مصرح لك باستخدام هذه اللوحة.", show_alert=True)
        return
    record_admin_action(
        getattr(getattr(update, "effective_user", None), "id", None),
        "callback:" + str(getattr(query, "data", "")).split(":", 1)[0],
        "authorized",
    )

    if not query.data:
        await query.answer("⚠️ بيانات الزر غير صالحة. افتح /menu من جديد.", show_alert=True)
        return

    msg_target = getattr(query, "message", None)
    if msg_target is None:
        await query.answer("⚠️ هذا الزر غير مرتبط برسالة قابلة للتحديث.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("res:"):
        try:
            page = max(0, int(data.split(":", 1)[1]))
        except ValueError:
            page = 0
        text, markup = await asyncio.to_thread(build_results_page, page)
        await msg_target.reply_text(text, reply_markup=markup)
        return

    if data.startswith("trade:"):
        await _send_trade_details(msg_target, data.split(":", 1)[1])
        return

    if data.startswith("scan:"):
        symbol = _resolve_input_symbol(data.split(":", 1)[1])
        if symbol is None:
            await msg_target.reply_text("⚠️ رمز الزوج في زر الفحص غير صالح.")
            return
        await msg_target.reply_text(f"⏳ جاري فحص {display_pair(symbol)}...")
        report = await _run_scan_in_worker(
            run_on_demand_scan, symbol, settings, operation="button scan"
        )
        has_signal = "🎯 التوصية: دخول صفقة" in report
        await msg_target.reply_text(
            _hide_trade_details_in_command_reply(report),
            reply_markup=_build_report_action_keyboard(symbol, has_signal),
        )

    elif data.startswith("pub:"):
        symbol = _resolve_input_symbol(data.split(":", 1)[1])
        if not symbol:
            await msg_target.reply_text("⚠️ رمز الزوج في زر النشر غير صالح.")
            return
        fetch_ticker = to_yfinance_ticker(symbol)
        primary_tf = settings.telegram_primary_timeframe
        outcome = await asyncio.to_thread(
            scan_regime_with_data, symbol, primary_tf, settings, fetch_symbol=fetch_ticker
        )
        if outcome:
            regime_result, df = outcome
            signal = await asyncio.to_thread(generate_signal, regime_result, df, settings)
            if signal:
                delivery = await asyncio.to_thread(
                    send_alert,
                    signal,
                    settings,
                    source="telegram-publish",
                    entry_bar_id=str(df.index[-1]),
                    market_frame=df,
                )
                if getattr(delivery, "blocked_reason", None):
                    await msg_target.reply_text(
                        f"⛔ لم تُنشر التوصية: {delivery.blocked_reason}"
                    )
                    return
                if delivery.get("duplicate"):
                    await msg_target.reply_text(
                        "ℹ️ لهذه التوصية سجل نشط أو نتيجة حديثة؛ لم أكرر نشرها."
                    )
                    return
                if delivery.get("telegram"):
                    await msg_target.reply_text(
                        f"✅ تم إرسال توصية {display_pair(symbol)} إلى القناة بنجاح!"
                    )
                else:
                    await msg_target.reply_text(
                        "❌ تعذر إرسال التوصية إلى قناة Telegram. تحقق من "
                        "TELEGRAM_CHANNEL_ID وصلاحية البوت في القناة."
                    )
                return
        await msg_target.reply_text("⚠️ لم تعد إشارة الدخول فعالة حالياً لإرسالها.")

    elif data.startswith("add:"):
        sym = _resolve_input_symbol(data.split(":", 1)[1])
        if sym is None:
            await msg_target.reply_text("⚠️ رمز الزوج في زر الإضافة غير صالح.")
            return
        symbol, was_new = watchlist_store.add(sym)
        if symbol is None:
            await msg_target.reply_text("⚠️ تعذر التحقق من رمز الزوج لإضافته.")
            return
        status_txt = f"✅ تمت إضافة {display_pair(symbol)}" if was_new else f"ℹ️ {display_pair(symbol)} مضاف مسبقاً"
        await msg_target.reply_text(status_txt, reply_markup=_build_add_symbols_inline_keyboard())

    elif data.startswith("rem:"):
        sym = _resolve_input_symbol(data.split(":", 1)[1])
        if sym is None:
            await msg_target.reply_text("⚠️ رمز الزوج في زر الحذف غير صالح.")
            return
        symbol, was_removed = watchlist_store.remove(sym)
        if symbol is None:
            await msg_target.reply_text("⚠️ تعذر التحقق من رمز الزوج لحذفه.")
            return
        status_txt = f"🗑️ تم حذف {display_pair(symbol)} من القائمة." if was_removed else "ℹ️ الزوج غير موجود."
        await msg_target.reply_text(status_txt, reply_markup=_build_watchlist_inline_keyboard())

    elif data.startswith("evol:"):
        parts = data.split(":")
        if len(parts) != 4:
            await msg_target.reply_text("⚠️ بيانات زر تطوير الاستراتيجية غير مكتملة. افتح /evolve من جديد.")
            return
        _, strat, raw_symbol, raw_timeframe = parts
        strat = strat.lower()
        symbol = _resolve_input_symbol(raw_symbol)
        timeframe = raw_timeframe.lower()
        valid_timeframes = {str(tf).lower() for tf in settings.timeframe_list}
        valid_timeframes.add(settings.telegram_primary_timeframe.lower())
        if strat not in evolver.PARAM_CLASSES or strat not in evolver.DEFAULT_GRIDS:
            await msg_target.reply_text("⚠️ الاستراتيجية في هذا الزر غير معروفة. افتح /evolve من جديد.")
            return
        if symbol is None:
            await msg_target.reply_text("⚠️ رمز الزوج في زر التطوير غير صالح.")
            return
        if timeframe not in valid_timeframes:
            await msg_target.reply_text("⚠️ الإطار الزمني في زر التطوير غير مدعوم.")
            return
        await msg_target.reply_text(
            f"🧪 جاري تشغيل اختبار {strat} على {display_pair(symbol)} ({timeframe.upper()})... انتظر قليلاً."
        )
        report = await asyncio.to_thread(run_evolution, strat, symbol, timeframe, settings)
        await msg_target.reply_text(report)

    elif data == "action:open_scan_menu":
        await msg_target.reply_text(
            "🔍 اختر الزوج الذي تريد فحصه بضغطة زر:",
            reply_markup=_build_quick_scan_inline_keyboard(),
        )

    elif data == "action:scan_all":
        await msg_target.reply_text("🚀 جاري فحص جميع الأزواج في قائمة المراقبة...")
        report = await _run_scan_in_worker(
            run_full_watchlist_scan, settings, operation="button full scan"
        )
        await msg_target.reply_text(
            _hide_trade_details_in_command_reply(report),
            reply_markup=_build_quick_scan_inline_keyboard(),
        )

    elif data == "action:watchlist":
        symbols = watchlist_store.get_all()
        await msg_target.reply_text(
            f"📋 قائمة المراقبة الحالية ({len(symbols)} أزواج):",
            reply_markup=_build_watchlist_inline_keyboard(),
        )

    elif data == "action:add_menu":
        await msg_target.reply_text(
            "➕ اضغط على أي زوج لإضافته إلى قائمة الفحص الدوري:",
            reply_markup=_build_add_symbols_inline_keyboard(),
        )

    else:
        await msg_target.reply_text(
            "⚠️ هذا الزر غير معروف أو انتهت صلاحيته. افتح /menu أو اطلب اللوحة مجدداً."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not update.message or not update.message.text:
        return
    if not _is_authorized(update, settings):
        await update.message.reply_text(
            "⚠️ غير مصرح لك باستخدام لوحة التحكم هذه." + _denial_hint(update)
        )
        return

    text = update.message.text.strip()

    # مطابقة مرنة للأزرار السفلية لضمان الاستجابة في جميع أجهزة الموبايل
    if text == BTN_QUICK_SCAN or "فحص سريع للأزواج" in text:
        context.args = []
        await cmd_scan(update, context)
        return
    if text == BTN_SCAN_ALL or "فحص شامل للسوق" in text:
        await cmd_scan_all(update, context)
        return
    if text == BTN_WATCHLIST or "قائمة المراقبة" in text:
        await cmd_watchlist(update, context)
        return
    if text == BTN_GOLD or "فحص الذهب" in text:
        await _handle_scan_request(update, settings, "XAUUSD")
        return
    if text == BTN_BTC or "فحص البيتكوين" in text:
        await _handle_scan_request(update, settings, "BTCUSD")
        return
    if text == BTN_BOT_STATUS or "حالة البوت" in text:
        await cmd_status(update, context)
        return
    if text == BTN_EVOLVE_MENU or "تطوير الاستراتيجيات" in text:
        context.args = []
        await cmd_evolve(update, context)
        return
    if text == BTN_RESULTS or "نتائج الصفقات" in text:
        context.args = []
        await cmd_results(update, context)
        return
    if text == BTN_HELP or "دليل الأوامر" in text:
        await cmd_start(update, context)
        return

    await _handle_scan_request(update, settings, text)


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not update.message or not await _require_authorized(update, settings, update.message):
        return
    await update.message.reply_text(
        f"⚠️ أمر غير معروف: {update.message.text}\n\n{_USAGE}",
        reply_markup=_build_main_reply_keyboard(),
    )


async def handle_telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log a safe error type and tell the user when a button/command failed."""
    error = getattr(context, "error", None)
    logger.error("Telegram update handler failed (%s)", type(error).__name__)
    message = getattr(update, "effective_message", None)
    if message is None:
        query = getattr(update, "callback_query", None)
        message = getattr(query, "message", None)
    if message is None or not hasattr(message, "reply_text"):
        return
    try:
        await message.reply_text(
            "⚠️ تعذر تنفيذ هذا الزر الآن بسبب خطأ داخلي. أعد المحاولة، "
            "وإذا تكرر الأمر فأرسل /status للمشرف."
        )
    except Exception as reply_error:
        logger.warning("could not report Telegram handler error (%s)", type(reply_error).__name__)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Available to everyone: shows the sender's numeric ID (needed for setup)."""
    settings = get_settings()
    msg_target = await _reply_target(update)
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    if msg_target is None or user is None:
        return
    configured = str(getattr(settings, "telegram_allowed_user_id", "") or "").strip()
    if not configured:
        state = "⚠️ لم يُضبط TELEGRAM_ALLOWED_USER_ID بعد على الخادم."
    elif configured == str(user.id):
        state = "✅ هذا الحساب هو المالك المصرّح له."
    else:
        state = "⛔ هذا الحساب ليس المالك المضبوط في TELEGRAM_ALLOWED_USER_ID."
    record_admin_action(getattr(user, "id", None), "/id", "info")
    await msg_target.reply_text(
        f"🆔 رقم حسابك في تيليجرام: {user.id}\n"
        f"💬 رقم هذه المحادثة: {getattr(chat, 'id', '')}\n"
        f"{state}"
    )


async def check_channel_permissions(bot, settings: Settings) -> str:
    """Verify read-only posting access for the explicit signal channel or group."""
    target = str(getattr(settings, "telegram_channel_id", "") or "").strip()
    if not target:
        return "📢 وجهة الإشارات: لم يُضبط TELEGRAM_CHANNEL_ID."
    if bot is None:
        return "📢 القناة: تعذر الفحص (البوت غير متصل)."
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(target)
        member = await bot.get_chat_member(target, me.id)
    except Exception as exc:
        return (
            f"📢 القناة {target}: تعذر التحقق ({type(exc).__name__}). "
            "تأكد من صحة المعرف ومن إضافة البوت إلى القناة."
        )
    status = str(getattr(member, "status", ""))
    if status == "creator":
        return f"📢 وجهة الإشارات {target}: البوت مالكها ✅"
    if status == "administrator":
        if getattr(chat, "type", "") in {"group", "supergroup"}:
            return f"📢 وجهة الإشارات {target}: البوت مشرف في المجموعة ويستطيع النشر ✅"
        if getattr(member, "can_post_messages", None) is not True:
            return f"📢 وجهة الإشارات {target}: تعذر إثبات صلاحية نشر الرسائل للمشرف ❌"
        return f"📢 وجهة الإشارات {target}: البوت مشرف ويستطيع النشر ✅"
    return f"📢 وجهة الإشارات {target}: البوت ليس مشرفاً (الحالة: {status or 'غير معروفة'}) ❌"


_OUTCOME_LABELS = {
    "TAKE_PROFIT_2": "✅ ربح (الهدف)",
    "STOP_LOSS": "❌ خسارة (الوقف)",
    "AMBIGUOUS": "⚠️ ملتبسة (الوقف والهدف بنفس الشمعة)",
    "REVERSED": "🔄 أُغلقت بإشارة معاكسة",
}


def _outcome_label(row: dict) -> str:
    if row.get("status") == "ACTIVE":
        return "⏳ مفتوحة"
    label = _OUTCOME_LABELS.get(str(row.get("outcome")), str(row.get("outcome")))
    if row.get("outcome") == "REVERSED" and row.get("r_multiple") is not None:
        label += " — ربح" if row["r_multiple"] > 0 else " — خسارة"
    return label


def _format_duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = [f"{v} {u}" for v, u in ((days, "يوم"), (hours, "ساعة"), (minutes, "دقيقة")) if v]
    return " ".join(parts) or "أقل من دقيقة"


def _format_summary_line(summary: dict) -> str:
    win_rate = f"{summary['win_rate_pct']:g}%" if summary.get("win_rate_pct") is not None else "—"
    avg = f"{summary['average_r']:+g}R" if summary.get("average_r") is not None else "—"
    return (
        f"📈 النتائج: مغلقة {summary['closed']} | ربح {summary['wins']} | خسارة {summary['losses']} | "
        f"ملتبسة {summary['ambiguous']} | مفتوحة {summary['active']}\n"
        f"🎯 نسبة الربح الفعلية: {win_rate} | مجموع R: {summary['total_r']:+g} | متوسط: {avg}"
    )


def build_results_page(page: int = 0):
    """Text + navigation keyboard for one page of recorded recommendations."""
    summary = signal_lifecycle.performance_summary()
    total = signal_lifecycle.count_signals()
    rows = signal_lifecycle.list_signals(limit=RESULTS_PAGE_SIZE, offset=page * RESULTS_PAGE_SIZE)
    lines = [
        "📊 سجل نتائج الصفقات (كل التوصيات المنشورة — لا تُخفى أي خسارة)",
        "----------------------------------",
        _format_summary_line(summary),
        "ℹ️ النتائج تقديرية من شموع OHLCV وليست تنفيذاً لدى وسيط؛ السبريد غير محتسب.",
        "----------------------------------",
    ]
    if not rows:
        lines.append("لا توجد صفقات مسجلة بعد. ستظهر هنا كل توصية تُنشر ونتيجتها.")
    for row in rows:
        r = f" | {row['r_multiple']:+g}R" if row.get("r_multiple") is not None else ""
        points = (
            f" | {row['points']:+g} {'نقطة' if row.get('points_unit') == 'pips' else 'وحدة سعر'}"
            if row.get("points") is not None else ""
        )
        lines.append(
            f"#{row['signal_id'][:8]} • {display_pair(str(row['symbol']).replace('=X', ''))} "
            f"{str(row['timeframe']).upper()} {row['direction']}\n"
            f"   {_outcome_label(row)}{r}{points}\n"
            f"   فُتحت: {str(row.get('opened_at') or '')[:16].replace('T', ' ')} UTC"
        )
    pages = max(1, (total + RESULTS_PAGE_SIZE - 1) // RESULTS_PAGE_SIZE)
    lines.append("----------------------------------")
    lines.append(f"صفحة {page + 1} من {pages} — اضغط على رقم الصفقة لعرض التفاصيل والشارت.")
    markup = None
    if InlineKeyboardMarkup is not None and InlineKeyboardButton is not None:
        buttons = [
            [InlineKeyboardButton(f"🔎 #{row['signal_id'][:8]} {row['direction']}", callback_data=f"trade:{row['signal_id'][:16]}")]
            for row in rows
        ]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ الأحدث", callback_data=f"res:{page - 1}"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton("الأقدم ➡️", callback_data=f"res:{page + 1}"))
        if nav:
            buttons.append(nav)
        markup = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), markup


def format_trade_details(row: dict) -> str:
    def price(value):
        return "—" if value is None else f"{float(value):g}"

    lines = [
        f"🧾 تفاصيل الصفقة #{row['signal_id'][:8]}",
        f"📌 {display_pair(str(row['symbol']).replace('=X', ''))} — {str(row['timeframe']).upper()} — {row['direction']}",
        f"🧠 الاستراتيجية: {row.get('strategy')} | المصدر: {row.get('source')}",
        f"⭐ تقييم الشروط: {row.get('quality_score') or '—'}/100 {row.get('quality_grade') or ''}",
        f"📍 الدخول: {price(row.get('entry_price'))}",
        f"🛑 وقف الخسارة: {price(row.get('stop_loss'))}",
        f"🎯 الهدف: {price(row.get('take_profit_2'))}",
        f"🕒 الفتح: {str(row.get('opened_at') or '')[:19].replace('T', ' ')} UTC",
        f"🏁 الحالة: {_outcome_label(row)}",
    ]
    if row.get("status") == "CLOSED":
        unit = "نقطة" if row.get("points_unit") == "pips" else "وحدة سعر"
        lines += [
            f"🚪 سعر الخروج: {price(row.get('exit_price'))}",
            f"🕒 الإغلاق: {str(row.get('closed_at') or '')[:19].replace('T', ' ')} UTC",
            f"⏱️ المدة: {_format_duration(row.get('duration_seconds'))}",
            f"📐 الحركة: {row['points']:+g} {unit}" if row.get("points") is not None else "📐 الحركة: غير محسوبة",
            f"💰 المحصلة: {row['r_multiple']:+g}R" if row.get("r_multiple") is not None else "💰 المحصلة: غير محسوبة",
        ]
    if row.get("strategy_version"):
        lines.append(f"🧬 نسخة الإعدادات: {row['strategy_version']}")
    lines.append(f"🆔 {row['signal_id']}")
    return "\n".join(lines)


async def _send_trade_details(msg_target, prefix: str) -> None:
    row = await asyncio.to_thread(signal_lifecycle.get_signal, prefix)
    if row is None:
        await msg_target.reply_text("⚠️ لم أجد صفقة بهذا الرقم. أرسل /results لعرض الأرقام.")
        return
    text = format_trade_details(row)
    chart_path = row.get("chart_path")
    if chart_path:
        from pathlib import Path

        path = Path(chart_path)
        if path.is_file():
            try:
                with path.open("rb") as image:
                    await msg_target.reply_photo(photo=image, caption=text[:1024])
                return
            except Exception as exc:
                logger.warning("could not send stored chart (%s)", type(exc).__name__)
    await msg_target.reply_text(text + "\n🖼️ صورة الشارت الأصلية غير متاحة على هذا الخادم.")


async def cmd_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    args = list(getattr(context, "args", None) or [])
    try:
        page = max(0, int(args[0]) - 1) if args else 0
    except ValueError:
        page = 0
    text, markup = await asyncio.to_thread(build_results_page, page)
    await msg_target.reply_text(text, reply_markup=markup)


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not msg_target or not await _require_authorized(update, settings, msg_target):
        return
    args = list(getattr(context, "args", None) or [])
    if not args:
        await msg_target.reply_text("اكتب رقم الصفقة بعد الأمر، مثال: /trade 1a2b3c4d\nأو أرسل /results لعرض الأرقام.")
        return
    await _send_trade_details(msg_target, args[0].lstrip("#"))


def build_telegram_application(settings: Settings | None = None) -> Application:
    if Application is None:
        raise RuntimeError(
            "Telegram integration requires the 'python-telegram-bot' package; "
            "install dependencies from requirements.txt."
        ) from _TELEGRAM_IMPORT_ERROR
    settings = settings or get_settings()
    # concurrent_updates: a slow /scan_all or /evolve must not block /start,
    # /status or button presses from other updates (previously every update
    # waited in one queue, which made the bot look unresponsive).
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(8)
        .build()
    )
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CommandHandler("results", cmd_results))
    application.add_handler(CommandHandler("trade", cmd_trade))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("scan_all", cmd_scan_all))
    application.add_handler(CommandHandler("watchlist", cmd_watchlist))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("remove", cmd_remove))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("channel_test", cmd_channel_test))
    application.add_handler(CommandHandler("paper_status", cmd_paper_status))
    application.add_handler(CommandHandler("evolve", cmd_evolve))
    application.add_handler(CommandHandler("apply", cmd_apply))
    if CallbackQueryHandler is not None:
        application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    application.add_error_handler(handle_telegram_error)
    return application
