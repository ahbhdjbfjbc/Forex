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
from src import indicators as ind
from src import oanda_client
from src.models import MarketRegime, RegimeResult
from src.notifier import (
    DIRECTION_EMOJI,
    DIRECTION_LABELS,
    REGIME_LABELS,
    STRATEGY_LABELS,
    dispatch_message,
    send_alert,
)
from src.oanda_client import PaperTradingConfigError, PaperTradingRequestError
from src.scanner_1_regime import scan_regime_with_data, scan_symbols_with_data
from src.scanner_2_strategy import generate_signal
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
    "• /scan — فتح قائمة الأزرار لفحص أي زوج بضغطة زر\n"
    "• /scan_all — فحص شامل لجميع الأزواج في القائمة الآن\n"
    "• /watchlist — عرض وإدارة قائمة المراقبة التلقائية (كل ساعة)\n"
    "• /add <الرمز> — إضافة زوج إلى قائمة الفحص التلقائي\n"
    "• /remove <الرمز> — حذف زوج من قائمة الفحص التلقائي\n"
    "• /status — عرض حالة البوت والإعدادات وفلتر الأخبار\n"
    "• /channel_test — اختبار إرسال رسالة إلى القناة\n"
    "• /paper_status — عرض رصيد الحساب التجريبي (OANDA) والصفقات المفتوحة\n"
    "• /evolve — اختبار وتطوير إعدادات الاستراتيجيات تاريخياً\n"
    "• /apply <رقم_المقترح> — اعتماد إعدادات استراتيجية ناجحة\n"
    "----------------------------------\n"
    f"{DEVELOPER_CREDIT}"
)


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
        [KeyboardButton(BTN_EVOLVE_MENU), KeyboardButton(BTN_HELP)],
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
        tp1_line = (
            f"🎯 الهدف الأول: {signal.take_profit_1_price} "
            f"(+{signal.take_profit_1_pips:g} نقطة | العائد 1:{signal.take_profit_1_rr:g})"
        )
        tp2_line = (
            f"🎯 الهدف الثاني: {signal.take_profit_2_price} "
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
        tp1_line = (
            f"🎯 الهدف الأول: {signal.take_profit_1_price} "
            f"(+{signal.take_profit_1_pips:g} | العائد 1:{signal.take_profit_1_rr:g})"
        )
        tp2_line = (
            f"🎯 الهدف الثاني: {signal.take_profit_2_price} "
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
            f"🎖️ درجة الدقة والثقة: {confidence_pct}%",
            f"🎯 التوصية: دخول صفقة {direction_label} {direction_emoji}",
            "",
            f"📈 سعر الدخول: {signal.entry_price}",
            sl_line,
            tp1_line,
            tp2_line,
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
        return _format_found_report(symbol, effective_primary_tf, scanned_tfs, regime_result, signal, df)
    return _format_no_trade_report(symbol, effective_primary_tf, scanned_tfs, regime_result)


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
        signal = generate_signal(regime_result, df, settings)
        if signal is not None and regime_result.confidence >= settings.min_signal_confidence:
            dir_emoji = DIRECTION_EMOJI.get(signal.direction, "")
            dir_label = DIRECTION_LABELS.get(signal.direction, signal.direction.value)
            conf_pct = int(round(regime_result.confidence * 100))
            found_signals.append(f"✅ {pair_str}: {dir_label} {dir_emoji} (الدقة: {conf_pct}%)")
            send_alert(signal, settings)
        else:
            reg_label = REGIME_LABELS.get(regime_result.regime, regime_result.regime.value)
            ranging_or_waiting.append(f"⏳ {pair_str}: {reg_label}")

    lines = [
        "🚀 تقرير الفحص الشامل للسوق 🚀",
        f"🔍 تم فحص ({len(regime_data)}) من الأزواج والأطر الزمنية:",
        "----------------------------------",
    ]
    if found_signals:
        lines.append("🎯 الفرص المكتملة (تم إرسالها للقناة):")
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
    """Authorize private users, but require an explicit user allowlist in groups."""
    chat = update.effective_chat
    if chat is None:
        return False

    allowed_user_id = str(getattr(settings, "telegram_allowed_user_id", "") or "").strip()
    user = update.effective_user
    configured_chat = str(settings.telegram_chat_id or "").strip()
    chat_type = getattr(chat, "type", "private")
    user_is_allowed = bool(
        allowed_user_id and user is not None and str(user.id) == allowed_user_id
    )

    if chat_type == "private":
        if allowed_user_id and not user_is_allowed:
            return False
        # A numeric positive ID is a configured private chat. Channel/group IDs
        # (negative) and @channel names must not block the owner's private UI.
        if configured_chat.isdigit() and not user_is_allowed:
            return str(chat.id) == configured_chat
        return True

    if chat_type in {"group", "supergroup"}:
        return bool(
            configured_chat
            and str(chat.id) == configured_chat
            and user_is_allowed
        )

    return False


async def _reply_target(update: Update):
    """يرجع الكائن المناسب للرد سواء كانت ضغطة زر أو رسالة عادية."""
    if getattr(update, "message", None) is not None:
        return update.message
    query = getattr(update, "callback_query", None)
    if query is not None and getattr(query, "message", None) is not None:
        return query.message
    return None


async def _handle_scan_request(update: Update, settings: Settings, raw_text: str) -> None:
    if not _is_authorized(update, settings):
        logger.warning("ignoring on-demand scan request from unauthorized Telegram sender")
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

    report = await asyncio.to_thread(run_on_demand_scan, raw_text, settings)
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

    await msg_target.reply_text(report, reply_markup=reply_markup)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not _is_authorized(update, settings):
        logger.warning("denying /start from an unauthorized Telegram sender")
        if msg_target:
            await msg_target.reply_text(
                "⛔ لا تملك صلاحية استخدام لوحة البوت. تحقق من "
                "TELEGRAM_ALLOWED_USER_ID أو افتح محادثة البوت الخاصة المسموح بها."
            )
        return
    if msg_target:
        await msg_target.reply_text(
            f"✅ بوت توصيات الفوركس الاحترافي متصل ويعمل بنجاح!\n\n{_USAGE}",
            reply_markup=_build_main_reply_keyboard(),
        )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not _is_authorized(update, settings) or not msg_target:
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
    if not _is_authorized(update, settings) or not msg_target:
        return
    wait_msg = await msg_target.reply_text(
        "🚀 جاري إجراء فحص شامل لجميع الأزواج في قائمة المراقبة... يرجى الانتظار لحظات."
    )
    report = await asyncio.to_thread(run_full_watchlist_scan, settings)
    try:
        await wait_msg.delete()
    except Exception:
        pass
    await msg_target.reply_text(report, reply_markup=_build_quick_scan_inline_keyboard())


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not _is_authorized(update, settings) or not msg_target:
        return
    symbols = watchlist_store.get_all()
    interval_hours = max(1, settings.auto_scan_interval_seconds // 3600)
    if not symbols:
        await msg_target.reply_text(
            "⚠️ قائمة الفحص التلقائي فارغة حالياً. اضغط على الزر بالأسفل لإضافة الأزواج الشهيرة فوراً:",
            reply_markup=_build_add_symbols_inline_keyboard(),
        )
        return
    lines = [
        f"📋 قائمة المراقبة التلقائية ({len(symbols)} أزواج — تُفحص كل {interval_hours} ساعة):",
        "اضغط على أي زوج لفحصه فوراً أو حذفه من القائمة:",
    ]
    await msg_target.reply_text(
        "\n".join(lines),
        reply_markup=_build_watchlist_inline_keyboard(),
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not _is_authorized(update, settings) or not msg_target:
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
    if symbol is None:
        await msg_target.reply_text(
            f"⚠️ الرمز '{raw}' غير صالح.\nاختر من القائمة التالية:",
            reply_markup=_build_add_symbols_inline_keyboard(),
        )
    elif was_new:
        await msg_target.reply_text(
            f"✅ تمت إضافة {display_pair(symbol)} إلى قائمة الفحص الدوري (كل ساعة).",
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
    if not _is_authorized(update, settings) or not msg_target:
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
    if not _is_authorized(update, settings) or not msg_target:
        return
    report = await asyncio.to_thread(_format_paper_status, settings)
    await msg_target.reply_text(report)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض حالة البوت والإعدادات الحالية وحالة الحساب التجريبي."""
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not _is_authorized(update, settings) or not msg_target:
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
        f"⏱️ دورة الفحص التلقائي: كل {settings.auto_scan_interval_seconds // 60} دقيقة ({settings.auto_scan_interval_seconds // 3600} ساعة)",
        f"🎯 الحد الأدنى لدقة التوصية: {conf_pct}%",
        f"📋 عدد الأزواج المراقبة: {len(symbols)} أزواج",
        f"🕒 الأطر الزمنية للفحص: {', '.join(settings.timeframe_list).upper()}",
        f"📢 قناة نشر التوصيات: {channel_status}",
        f"📰 فلتر التوقف وقت الأخبار: {news_status}",
        f"⚠️ تنبيه التذبذب وعدم وجود فرص: {no_sig_status}",
        f"💼 إدارة المخاطر: {settings.risk_percent_per_trade:g}% من رصيد ${settings.account_size_usd:,.0f}",
        "----------------------------------",
    ]
    paper_text = await asyncio.to_thread(_format_paper_status, settings)
    lines.append(paper_text)
    lines.append("----------------------------------")
    lines.append(DEVELOPER_CREDIT)
    await msg_target.reply_text("\n".join(lines), reply_markup=_build_main_reply_keyboard())


async def cmd_channel_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل رسالة اختبارية للقناة للتأكد من صلاحيات البوت."""
    settings = get_settings()
    msg_target = await _reply_target(update)
    if not _is_authorized(update, settings) or not msg_target:
        return
    test_msg = (
        "✅ رسالة اختبار من بوت توصيات الفوركس\n"
        "تم ربط البوت بالقناة بنجاح، وستصل التوصيات وتنبيهات الأخبار وحالة السوق هنا تلقائياً كل ساعة.\n"
        "------------------------------\n"
        f"{DEVELOPER_CREDIT}"
    )
    res = await asyncio.to_thread(dispatch_message, test_msg, settings)
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
    baseline = asdict(get_strategy_params(strategy_name))
    change_lines = [f"{key}: {baseline.get(key)} -> {new_val}" for key, new_val in candidate.param_overrides.items()]
    changes_text = "; ".join(change_lines) if change_lines else "لا توجد تغييرات عن الإعدادات الافتراضية"

    pf = r.profit_factor
    pf_display = f"{pf:.2f}" if isinstance(pf, float) and pf != float("inf") else "∞"

    if candidate.passed:
        status_line = (
            f"✅ النتيجة: اجتاز معايير القبول بنجاح!\n"
            f"لتطبيق هذه الإعدادات أرسل الأمر: /apply {candidate.proposal_id}"
        )
    else:
        status_line = (
            f"❌ النتيجة: لم يجتز معايير الأمان الصارمة (يتطلب Sharpe>{evolver.ACCEPTANCE_MIN_SHARPE}, "
            f"PF>{evolver.ACCEPTANCE_MIN_PROFIT_FACTOR}, وأقصى تراجع أقل من {evolver.ACCEPTANCE_MAX_DRAWDOWN_PCT}%) — لم يتم التطبيق."
        )

    return "\n".join(
        [
            "🧪 تقرير تطوير واختبار الاستراتيجية (Backtest)",
            "----------------------------------",
            f"🧠 الاستراتيجية: {evolver.STRATEGY_DISPLAY_LABELS.get(strategy_name, strategy_name)}",
            f"📌 الزوج: {display_pair(symbol)} ({timeframe.upper()})",
            "",
            f"📊 أفضل نتيجة من بين ({tested_count}) اختبار "
            f"(من {r.start.date()} إلى {r.end.date()}):",
            f"• نسبة النجاح (Win Rate): {r.win_rate_pct:.1f}%",
            f"• معامل الربح (Profit Factor): {pf_display}",
            f"• نسبة شارب (Sharpe Ratio): {r.sharpe_ratio:.2f}",
            f"• أقصى تراجع (Max Drawdown): -{r.max_drawdown_pct:.1f}%",
            f"• إجمالي الصفقات المختبرة: {r.total_trades}",
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
    if not _is_authorized(update, settings) or not msg_target:
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
    if not _is_authorized(update, settings) or not msg_target:
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
    if query is None or not query.data:
        return

    if not _is_authorized(update, settings):
        await query.answer("⚠️ غير مصرح لك باستخدام هذه اللوحة.", show_alert=True)
        return

    await query.answer()
    data = query.data
    msg_target = query.message

    if data.startswith("scan:"):
        symbol = data.split(":", 1)[1]
        await msg_target.reply_text(f"⏳ جاري فحص {display_pair(symbol)}...")
        report = await asyncio.to_thread(run_on_demand_scan, symbol, settings)
        has_signal = "🎯 التوصية: دخول صفقة" in report
        await msg_target.reply_text(
            report, reply_markup=_build_report_action_keyboard(symbol, has_signal)
        )

    elif data.startswith("pub:"):
        symbol = _resolve_input_symbol(data.split(":", 1)[1])
        if not symbol:
            return
        fetch_ticker = to_yfinance_ticker(symbol)
        primary_tf = settings.telegram_primary_timeframe
        outcome = await asyncio.to_thread(
            scan_regime_with_data, symbol, primary_tf, settings, fetch_symbol=fetch_ticker
        )
        if outcome:
            regime_result, df = outcome
            signal = generate_signal(regime_result, df, settings)
            if signal:
                await asyncio.to_thread(send_alert, signal, settings)
                await msg_target.reply_text(f"✅ تم إرسال توصية {display_pair(symbol)} إلى القناة بنجاح!")
                return
        await msg_target.reply_text("⚠️ لم تعد إشارة الدخول فعالة حالياً لإرسالها.")

    elif data.startswith("add:"):
        sym = data.split(":", 1)[1]
        symbol, was_new = watchlist_store.add(sym)
        status_txt = f"✅ تمت إضافة {display_pair(symbol)}" if was_new else f"ℹ️ {display_pair(symbol)} مضاف مسبقاً"
        await msg_target.reply_text(status_txt, reply_markup=_build_add_symbols_inline_keyboard())

    elif data.startswith("rem:"):
        sym = data.split(":", 1)[1]
        symbol, was_removed = watchlist_store.remove(sym)
        status_txt = f"🗑️ تم حذف {display_pair(symbol)} من القائمة." if was_removed else "ℹ️ الزوج غير موجود."
        await msg_target.reply_text(status_txt, reply_markup=_build_watchlist_inline_keyboard())

    elif data.startswith("evol:"):
        parts = data.split(":")
        if len(parts) == 4:
            _, strat, sym, tf = parts
            await msg_target.reply_text(
                f"🧪 جاري تشغيل اختبار {strat} على {display_pair(sym)} ({tf.upper()})... انتظر قليلاً."
            )
            report = await asyncio.to_thread(run_evolution, strat, sym, tf, settings)
            await msg_target.reply_text(report)

    elif data == "action:open_scan_menu":
        await msg_target.reply_text(
            "🔍 اختر الزوج الذي تريد فحصه بضغطة زر:",
            reply_markup=_build_quick_scan_inline_keyboard(),
        )

    elif data == "action:scan_all":
        await msg_target.reply_text("🚀 جاري فحص جميع الأزواج في قائمة المراقبة...")
        report = await asyncio.to_thread(run_full_watchlist_scan, settings)
        await msg_target.reply_text(report, reply_markup=_build_quick_scan_inline_keyboard())

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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not update.message or not update.message.text:
        return
    if not _is_authorized(update, settings):
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
    if text == BTN_HELP or "دليل الأوامر" in text:
        await cmd_start(update, context)
        return

    await _handle_scan_request(update, settings, text)


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return
    await update.message.reply_text(
        f"⚠️ أمر غير معروف: {update.message.text}\n\n{_USAGE}",
        reply_markup=_build_main_reply_keyboard(),
    )


def build_telegram_application(settings: Settings | None = None) -> Application:
    if Application is None:
        raise RuntimeError(
            "Telegram integration requires the 'python-telegram-bot' package; "
            "install dependencies from requirements.txt."
        ) from _TELEGRAM_IMPORT_ERROR
    settings = settings or get_settings()
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", cmd_start))
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
    return application
