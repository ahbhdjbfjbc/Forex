"""On-demand Telegram interface — type a ticker, get an immediate scan.

Owns: the Telegram polling listener (commands + free text) and the
scan-report message format. It calls Scanner 1 / Scanner 2's existing public
entry points for the actual regime/strategy math — no indicator or strategy
logic lives here. Distinct from `src/notifier.py`, which only pushes
pre-built `TradeSignal`s outward; this module runs a fresh scan per request
and builds its own report format (the interactive "SCAN REPORT" template),
not the push-alert one.

Only the chat configured as `TELEGRAM_CHAT_ID` is served — this is a personal
bot wired to one account's risk settings, not a public service, so any other
chat that messages it is ignored (see `_is_authorized`).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
    _TELEGRAM_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # keep scanner/report logic importable without the optional runtime adapter
    # python-telegram-bot remains required to poll Telegram. Deferring the
    # failure lets the scanning/backtesting modules and their tests run in
    # minimal environments where the Telegram integration is not installed.
    Update = object  # annotations are postponed by `from __future__ import annotations`
    Application = None
    CommandHandler = None
    ContextTypes = None
    MessageHandler = None
    filters = None
    _TELEGRAM_IMPORT_ERROR = exc

import strategy_evolver as evolver
from config import Settings, get_settings
from src import indicators as ind
from src import oanda_client
from src.models import MarketRegime, RegimeResult
from src.notifier import DIRECTION_EMOJI, DIRECTION_LABELS, REGIME_LABELS, STRATEGY_LABELS
from src.oanda_client import PaperTradingConfigError, PaperTradingRequestError
from src.scanner_1_regime import scan_regime_with_data
from src.scanner_2_strategy import generate_signal
from src.strategy_params import get_strategy_params
from src.symbol_resolver import display_pair, instrument_type, sanitize_symbol, to_yfinance_ticker
from src.watchlist_store import watchlist_store

logger = logging.getLogger(__name__)

_NO_TRADE_REASONS = {
    MarketRegime.LOW_LIQUIDITY_CHOP: (
        "السيولة ضعيفة والسوق متذبذب؛ انتظر تحسّن السيولة وظهور اختراق واضح."
    ),
    MarketRegime.RANGING_CONSOLIDATION: (
        "السعر داخل نطاق عرضي، ولا يوجد حالياً توافق كافٍ بين نطاقات بولينجر ومؤشر القوة النسبية (RSI)."
    ),
    MarketRegime.STRONG_TREND_BULLISH: (
        "الاتجاه صاعد، لكن لم يظهر ارتداد مؤكد إلى المتوسط المتحرك الأسي 20؛ انتظر فرصة أوضح."
    ),
    MarketRegime.STRONG_TREND_BEARISH: (
        "الاتجاه هابط، لكن لم يظهر ارتداد مؤكد إلى المتوسط المتحرك الأسي 20؛ انتظر فرصة أوضح."
    ),
    MarketRegime.HIGH_VOLATILITY_EXPANSION: (
        "التقلبات مرتفعة، لكن لم يتأكد اختراق النطاق مع حجم تداول داعم. انتظر تأكيداً."
    ),
}

_USAGE = (
    "أرسل رمز زوج أو أصل، مثل EURUSD أو XAUUSD أو GBPJPY، أو استخدم /scan <الرمز>.\n"
    "تعمل أيضاً أسماء مثل gold للذهب وcable لزوج GBP/USD.\n\n"
    "يُضاف كل رمز تفحصه تلقائياً إلى قائمة المسح كل 3 ساعات. الأوامر:\n"
    "/watchlist — عرض القائمة\n"
    "/add <الرمز> — إضافة رمز\n"
    "/remove <الرمز> — إزالة رمز\n"
    "/paper_status — حالة حساب OANDA التجريبي\n"
    "/evolve <الاستراتيجية> <الرمز> [الفاصل] — اختبار إعدادات بديلة\n"
    "أسماء الاستراتيجيات: trend_pullback أو mean_reversion أو breakout.\n"
    "/apply <معرّف> — تطبيق اقتراح اجتاز معايير الاختبار"
)

_STRATEGY_NAME_AR = {
    "trend_pullback": "ارتداد مع الاتجاه",
    "mean_reversion": "العودة إلى المتوسط",
    "breakout": "اختراق التقلبات",
}

_PARAMETER_NAME_AR = {
    "rsi_bullish_ceiling": "الحد الأعلى للقوة النسبية في الاتجاه الصاعد",
    "atr_sl_multiplier": "مضاعف متوسط المدى الحقيقي لوقف الخسارة",
    "rsi_oversold": "حد التشبع البيعي لمؤشر القوة النسبية",
    "rsi_overbought": "حد التشبع الشرائي لمؤشر القوة النسبية",
    "breakout_rvol_min": "الحد الأدنى للحجم النسبي للاختراق",
}


def _format_found_report(
    symbol: str, primary_tf: str, scanned_tfs: list[str], regime_result: RegimeResult, signal, df
) -> str:
    kind = instrument_type(symbol)
    rsi_val = float(ind.rsi(df["Close"], 14).iloc[-1])
    direction_emoji = DIRECTION_EMOJI.get(signal.direction, "")
    strategy_label = STRATEGY_LABELS.get(signal.strategy, signal.strategy.value)
    tf_tag = "/".join(tf.upper() for tf in scanned_tfs) or primary_tf.upper()

    if kind == "FX":
        sl_line = f"🛑 وقف الخسارة: {signal.stop_loss_price} (-{signal.stop_loss_pips:g} نقطة)"
        tp1_line = (
            f"🎯 الهدف الأول: {signal.take_profit_1_price} "
            f"(+{signal.take_profit_1_pips:g} نقطة | عائد/مخاطرة 1:{signal.take_profit_1_rr:g})"
        )
        tp2_line = (
            f"🎯 الهدف الثاني: {signal.take_profit_2_price} "
            f"(+{signal.take_profit_2_pips:g} نقطة | عائد/مخاطرة 1:{signal.take_profit_2_rr:g})"
        )
        size_line = (
            f"📦 حجم الصفقة المقترح: {signal.risk.lot_size:g} لوت "
            f"(مخاطرة {signal.risk.risk_percent:g}% من حساب ${signal.risk.account_size_usd:,.0f})"
        )
    else:
        unit_label = "بيتكوين" if symbol.startswith("BTC") else "إيثيريوم" if symbol.startswith("ETH") else \
            "أونصة" if kind == "METAL" else "وحدة"
        sl_line = f"🛑 وقف الخسارة: {signal.stop_loss_price} (مسافة السعر {signal.stop_loss_pips:g})"
        tp1_line = (
            f"🎯 الهدف الأول: {signal.take_profit_1_price} "
            f"(مسافة السعر {signal.take_profit_1_pips:g} | عائد/مخاطرة 1:{signal.take_profit_1_rr:g})"
        )
        tp2_line = (
            f"🎯 الهدف الثاني: {signal.take_profit_2_price} "
            f"(مسافة السعر {signal.take_profit_2_pips:g} | عائد/مخاطرة 1:{signal.take_profit_2_rr:g})"
        )
        size_line = (
            f"📦 الحجم الاسترشادي: {signal.risk.lot_size:g} {unit_label} "
            f"(مخاطرة {signal.risk.risk_percent:g}% من حساب ${signal.risk.account_size_usd:,.0f})"
        )

    return "\n".join(
        [
            f"📊 تقرير تحليل: {display_pair(symbol)}",
            "----------------------------------",
            f"🔍 حالة السوق: {REGIME_LABELS.get(regime_result.regime, regime_result.regime.value)} ({tf_tag})",
            f"⚡ الاستراتيجية: {strategy_label}",
            f"🎯 التوصية: {DIRECTION_LABELS.get(signal.direction, signal.direction.value)} {direction_emoji}",
            "",
            f"📈 سعر الدخول: {signal.entry_price}",
            sl_line,
            tp1_line,
            tp2_line,
            size_line,
            "----------------------------------",
            f"💡 المؤشرات: قوة الاتجاه (ADX) = {regime_result.metrics.adx:.1f} | "
            f"متوسط المدى الحقيقي (ATR) = {regime_result.metrics.atr:.5f} | "
            f"القوة النسبية (RSI) = {rsi_val:.0f}",
        ]
    )


def _format_no_trade_report(
    symbol: str, primary_tf: str, scanned_tfs: list[str], regime_result: RegimeResult
) -> str:
    tf_tag = "/".join(tf.upper() for tf in scanned_tfs) or primary_tf.upper()
    reason = _NO_TRADE_REASONS.get(
        regime_result.regime, "لا يوجد حالياً محفّز دخول مؤكد. انتظر تحسن ظروف السوق."
    )
    return "\n".join(
        [
            f"📊 تقرير تحليل: {display_pair(symbol)}",
            "----------------------------------",
            f"🔍 حالة السوق: {REGIME_LABELS.get(regime_result.regime, regime_result.regime.value)} ({tf_tag})",
            "⚡ الاستراتيجية: لا توجد استراتيجية مناسبة الآن",
            "⚠️ القرار: لا تدخل صفقة حالياً 🔴",
            "",
            f"💡 السبب: {reason}",
        ]
    )


def run_on_demand_scan(raw_text: str, settings: Settings | None = None) -> str:
    """Sanitize free-text input, scan it, and return a formatted report
    string ready to send. Synchronous and network-bound (yfinance) — call
    via `asyncio.to_thread` from the async Telegram handler so it doesn't
    block the bot's event loop."""
    settings = settings or get_settings()

    symbol = sanitize_symbol(raw_text)
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
            f"📊 تقرير تحليل: {display_pair(symbol)}\n"
            "----------------------------------\n"
            f"⚠️ تعذّر جلب بيانات السوق للرمز {symbol} ({fetch_ticker}). "
            "تحقق من الرمز أو أعد المحاولة لاحقاً؛ قد يكون مصدر البيانات متوقفاً مؤقتاً."
        )

    watchlist_store.add(symbol)  # requesting a scan implies interest — auto-track it going forward

    effective_primary_tf = primary_tf if primary_tf in scanned else next(iter(scanned))
    regime_result, df = scanned[effective_primary_tf]
    scanned_tfs = list(scanned.keys())

    signal = generate_signal(regime_result, df, settings)
    if signal is not None:
        return _format_found_report(symbol, effective_primary_tf, scanned_tfs, regime_result, signal, df)
    return _format_no_trade_report(symbol, effective_primary_tf, scanned_tfs, regime_result)


def _is_authorized(update: Update, settings: Settings) -> bool:
    chat = update.effective_chat
    if chat is None or not settings.telegram_chat_id:
        return False
    if str(chat.id) != str(settings.telegram_chat_id):
        return False

    allowed_user_id = str(getattr(settings, "telegram_allowed_user_id", "") or "").strip()
    user = update.effective_user
    if allowed_user_id:
        return user is not None and str(user.id) == allowed_user_id

    # A group/supergroup chat ID authenticates the whole room, not the sender.
    # Fail closed unless a specific user ID is configured for group commands.
    return getattr(chat, "type", "private") == "private"


async def _handle_scan_request(update: Update, settings: Settings, raw_text: str) -> None:
    if not _is_authorized(update, settings):
        logger.warning("ignoring on-demand scan request from unauthorized Telegram sender")
        return
    if not update.message:
        return

    report = await asyncio.to_thread(run_on_demand_scan, raw_text, settings)
    await update.message.reply_text(report)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings):
        return
    if update.message:
        await update.message.reply_text(f"بوت تحليل الفوركس يعمل.\n{_USAGE}")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    raw_text = " ".join(context.args) if context.args else ""
    if not raw_text and update.message:
        await update.message.reply_text(_USAGE)
        return
    await _handle_scan_request(update, settings, raw_text)


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return
    symbols = watchlist_store.get_all()
    if not symbols:
        await update.message.reply_text("قائمة المسح فارغة. أضف رمزاً باستخدام /add <الرمز>.")
        return
    lines = [f"📋 قائمة الرموز ({len(symbols)}، مسح كل {settings.auto_scan_interval_seconds // 3600} ساعة):"]
    lines.extend(f"• {display_pair(s)}" for s in symbols)
    await update.message.reply_text("\n".join(lines))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return
    raw = " ".join(context.args) if context.args else ""
    symbol, was_new = watchlist_store.add(raw)
    if symbol is None:
        await update.message.reply_text(f"الرمز '{raw}' غير معروف أو غير صالح.\n{_USAGE}")
    elif was_new:
        await update.message.reply_text(f"✅ أُضيف {display_pair(symbol)} إلى قائمة المسح كل 3 ساعات.")
    else:
        await update.message.reply_text(f"{display_pair(symbol)} موجود بالفعل في قائمة المسح.")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return
    raw = " ".join(context.args) if context.args else ""
    symbol, was_removed = watchlist_store.remove(raw)
    if symbol is None:
        await update.message.reply_text(f"الرمز '{raw}' غير معروف أو غير صالح.\n{_USAGE}")
    elif was_removed:
        await update.message.reply_text(f"🗑️ أُزيل {display_pair(symbol)} من قائمة المسح.")
    else:
        await update.message.reply_text(f"{display_pair(symbol)} غير موجود في قائمة المسح.")


def _format_paper_status(settings: Settings) -> str:
    if not settings.enable_paper_trading:
        return "التداول التجريبي مع OANDA غير مفعّل (ENABLE_PAPER_TRADING=false)."
    try:
        summary = oanda_client.get_account_summary(settings)
        trades = oanda_client.get_open_trades(settings)
    except (PaperTradingConfigError, PaperTradingRequestError) as exc:
        logger.warning("could not read OANDA practice account (%s)", type(exc).__name__)
        return "⚠️ تعذّر الوصول إلى حساب OANDA التجريبي. راجع إعدادات الحساب وسجلات الخدمة."

    account = summary.get("account", {})
    lines = [
        "📄 حالة التداول التجريبي (OANDA)",
        f"الرصيد: {account.get('balance')} {account.get('currency', '')}",
        f"الربح/الخسارة غير المحققة: {account.get('unrealizedPL')}",
        f"الصفقات المفتوحة: {len(trades)}",
    ]
    for t in trades[:10]:
        lines.append(f"  • {t.get('instrument')}، الوحدات {t.get('currentUnits')}، السعر {t.get('price')}")
    return "\n".join(lines)


async def cmd_paper_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return
    report = await asyncio.to_thread(_format_paper_status, settings)
    await update.message.reply_text(report)


def _format_evolution_report(
    strategy_name: str, symbol: str, timeframe: str, candidate, tested_count: int
) -> str:
    r = candidate.result
    baseline = asdict(get_strategy_params(strategy_name))
    change_lines = [
        f"{_PARAMETER_NAME_AR.get(key, key)}: {baseline.get(key)} ← {new_val}"
        for key, new_val in candidate.param_overrides.items()
    ]
    changes_text = "؛ ".join(change_lines) if change_lines else "لا تغييرات عن الإعدادات الحالية"

    pf = r.profit_factor
    pf_display = f"{pf:.2f}" if isinstance(pf, float) and pf != float("inf") else "∞"

    if candidate.passed:
        status_line = f"✅ الحالة: اجتاز معايير القبول. لتطبيقه أرسل /apply {candidate.proposal_id}."
    else:
        status_line = (
            f"❌ الحالة: لم يجتز معايير القبول (Sharpe>{evolver.ACCEPTANCE_MIN_SHARPE}، "
            f"معامل الربح>{evolver.ACCEPTANCE_MIN_PROFIT_FACTOR}، "
            f"والسحب الأقصى<{evolver.ACCEPTANCE_MAX_DRAWDOWN_PCT}%)؛ لم تُطبّق التغييرات."
        )

    return "\n".join(
        [
            "🧪 تقرير اختبار بدائل الاستراتيجية",
            "----------------------------------",
            f"الاستراتيجية: {_STRATEGY_NAME_AR.get(strategy_name, strategy_name)}",
            f"الأصل: {display_pair(symbol)} ({timeframe.upper()})",
            "",
            f"📊 أفضل نتيجة من {tested_count} اختبارات للفترة {r.start.date()} إلى {r.end.date()}:",
            f"• نسبة الصفقات الرابحة: {r.win_rate_pct:.1f}%",
            f"• معامل الربح: {pf_display}",
            f"• نسبة Sharpe: {r.sharpe_ratio:.2f}",
            f"• أقصى تراجع: -{r.max_drawdown_pct:.1f}%",
            f"• عدد الصفقات: {r.total_trades}",
            "",
            status_line,
            "----------------------------------",
            f"💡 التغييرات المختبرة: {changes_text}",
            f"معرّف الاقتراح: {candidate.proposal_id}",
        ]
    )


def run_evolution(strategy_name: str, symbol: str, timeframe: str, settings: Settings) -> str:
    """Backtests `strategy_evolver.DEFAULT_GRIDS[strategy_name]` for
    `symbol`/`timeframe` and formats a report for the best (highest-Sharpe)
    candidate. Every candidate is saved as a proposal regardless of outcome
    — nothing here writes to `data/strategy_params.json`; that only happens
    via an explicit `/apply <proposal_id>`."""
    grid = evolver.DEFAULT_GRIDS.get(strategy_name, {})
    try:
        candidates = evolver.evolve_strategy(strategy_name, symbol, timeframe, grid, settings, lookback_months=6)
    except Exception:
        logger.exception("evolution run failed for %s/%s/%s", strategy_name, symbol, timeframe)
        return f"⚠️ تعذّر اختبار الاستراتيجية لـ{display_pair(symbol)} ({timeframe.upper()}). راجع سجلات الخدمة."

    if not candidates:
        return (
            f"🧪 تقرير اختبار بدائل الاستراتيجية\n----------------------------------\n"
            f"الاستراتيجية: {_STRATEGY_NAME_AR.get(strategy_name, strategy_name)}\n"
            f"الأصل: {display_pair(symbol)} ({timeframe.upper()})\n\n"
            "⚠️ لم تتوفر نتيجة اختبار؛ البيانات التاريخية غير كافية لهذا الرمز والفاصل."
        )

    best = candidates[0]  # evolve_strategy already sorts best-Sharpe-first
    return _format_evolution_report(strategy_name, symbol, timeframe, best, len(candidates))


async def cmd_evolve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "طريقة الاستخدام: /evolve <الاستراتيجية> <الرمز> [الفاصل]\n"
            f"المعرّفات المقبولة: {', '.join(sorted(evolver.PARAM_CLASSES))}\n"
            "مثال: /evolve trend_pullback EURUSD 1h\n"
            "يستخدم البوت مجموعة إعدادات جاهزة؛ للاختبار المخصص استخدم strategy_evolver.py."
        )
        return

    strategy_name = args[0].lower()
    if strategy_name not in evolver.PARAM_CLASSES:
        await update.message.reply_text(
            f"الاستراتيجية '{args[0]}' غير معروفة. المعرّفات المقبولة: {', '.join(sorted(evolver.PARAM_CLASSES))}"
        )
        return

    symbol = sanitize_symbol(args[1])
    if symbol is None:
        await update.message.reply_text(f"الرمز '{args[1]}' غير معروف أو غير صالح.")
        return

    timeframe = args[2].lower() if len(args) > 2 else settings.telegram_primary_timeframe

    grid = evolver.DEFAULT_GRIDS.get(strategy_name, {})
    combo_count = 1
    for values in grid.values():
        combo_count *= len(values)

    await update.message.reply_text(
        f"🧪 جارٍ اختبار {_STRATEGY_NAME_AR.get(strategy_name, strategy_name)} "
        f"على {display_pair(symbol)} ({timeframe.upper()}) عبر {combo_count} تركيبة إعدادات "
        "لستة أشهر. قد يستغرق ذلك بضع دقائق..."
    )

    report = await asyncio.to_thread(run_evolution, strategy_name, symbol, timeframe, settings)
    await update.message.reply_text(report)


async def cmd_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("طريقة الاستخدام: /apply <معرّف الاقتراح> (الموجود في تقرير /evolve).")
        return

    proposal_id = args[0]
    proposal = evolver.load_proposal(proposal_id)
    if proposal is None:
        await update.message.reply_text(f"لم يُعثر على اقتراح بالمعرّف {proposal_id}.")
        return

    applied = await asyncio.to_thread(evolver.apply_proposal, proposal_id)
    if applied:
        await update.message.reply_text(
            f"✅ طُبّق الاقتراح {proposal_id} على {_STRATEGY_NAME_AR.get(proposal['strategy_name'], proposal['strategy_name'])}. "
            "ستستخدم الإشارات الإعدادات الجديدة ابتداءً من المسح التالي. "
            "حُفظت نسخة احتياطية من الإعدادات السابقة."
        )
    else:
        await update.message.reply_text(
            f"❌ لم يُطبّق الاقتراح {proposal_id} لأنه لم يجتز معايير القبول."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not update.message or not update.message.text:
        return
    await _handle_scan_request(update, settings, update.message.text)


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches any /command that doesn't match a registered handler (e.g. a
    typo like /wishlist) so it gets a helpful reply instead of being
    silently dropped — CommandHandler only fires on an exact name match,
    and there's no built-in fallback for unmatched commands."""
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return
    await update.message.reply_text(
        f"الأمر غير معروف: {update.message.text}\n{_USAGE}"
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
    application.add_handler(CommandHandler("watchlist", cmd_watchlist))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("remove", cmd_remove))
    application.add_handler(CommandHandler("paper_status", cmd_paper_status))
    application.add_handler(CommandHandler("evolve", cmd_evolve))
    application.add_handler(CommandHandler("apply", cmd_apply))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    return application
