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
from src.notifier import DIRECTION_EMOJI, STRATEGY_LABELS
from src.oanda_client import PaperTradingConfigError, PaperTradingRequestError
from src.scanner_1_regime import scan_regime_with_data
from src.scanner_2_strategy import generate_signal
from src.strategy_params import get_strategy_params
from src.symbol_resolver import display_pair, instrument_type, sanitize_symbol, to_yfinance_ticker
from src.watchlist_store import watchlist_store

logger = logging.getLogger(__name__)

_NO_TRADE_REASONS = {
    MarketRegime.LOW_LIQUIDITY_CHOP: (
        "Market is in consolidation with low volume. Wait for a breakout before taking entries."
    ),
    MarketRegime.RANGING_CONSOLIDATION: (
        "Price is inside the range — no Bollinger Band extreme + RSI overbought/oversold "
        "confluence right now."
    ),
    MarketRegime.STRONG_TREND_BULLISH: (
        "Trend is bullish but price hasn't pulled back to EMA20 with RSI/fib confirmation "
        "yet — wait for a retracement entry."
    ),
    MarketRegime.STRONG_TREND_BEARISH: (
        "Trend is bearish but price hasn't pulled back to EMA20 with RSI/fib confirmation "
        "yet — wait for a retracement entry."
    ),
    MarketRegime.HIGH_VOLATILITY_EXPANSION: (
        "Volatility is expanding but price hasn't broken the Donchian channel with volume "
        "confirmation yet."
    ),
}

_USAGE = (
    "Send a pair or ticker (e.g. EURUSD, XAUUSD, GBPJPY, BTCUSD) or use /scan <symbol>.\n"
    "Aliases like gold, cable, and bitcoin also work.\n\n"
    "Every symbol you scan is auto-added to the 3-hour watchlist. Manage it with:\n"
    "/watchlist — see what's being auto-scanned\n"
    "/add <symbol> — track a symbol\n"
    "/remove <symbol> — stop tracking a symbol\n"
    "/paper_status — OANDA practice account balance & open trades\n"
    "/evolve <strategy> <symbol> [timeframe] — backtest parameter variants "
    "(strategies: trend_pullback, mean_reversion, breakout)\n"
    "/apply <proposal_id> — apply a passing /evolve proposal to the live strategy"
)


def _format_found_report(
    symbol: str, primary_tf: str, scanned_tfs: list[str], regime_result: RegimeResult, signal, df
) -> str:
    kind = instrument_type(symbol)
    rsi_val = float(ind.rsi(df["Close"], 14).iloc[-1])
    direction_emoji = DIRECTION_EMOJI.get(signal.direction, "")
    strategy_label = STRATEGY_LABELS.get(signal.strategy, signal.strategy.value)
    tf_tag = "/".join(tf.upper() for tf in scanned_tfs) or primary_tf.upper()

    if kind == "FX":
        sl_line = f"🛑 Stop Loss: {signal.stop_loss_price} (-{signal.stop_loss_pips:g} pips)"
        tp1_line = (
            f"🎯 Take Profit 1: {signal.take_profit_1_price} "
            f"(+{signal.take_profit_1_pips:g} pips | R:R 1:{signal.take_profit_1_rr:g})"
        )
        tp2_line = (
            f"🎯 Take Profit 2: {signal.take_profit_2_price} "
            f"(+{signal.take_profit_2_pips:g} pips | R:R 1:{signal.take_profit_2_rr:g})"
        )
        size_line = (
            f"📦 Suggested Risk: {signal.risk.lot_size:g} Lots "
            f"({signal.risk.risk_percent:g}% Risk on ${signal.risk.account_size_usd:,.0f})"
        )
    else:
        unit_label = "BTC" if symbol.startswith("BTC") else "ETH" if symbol.startswith("ETH") else \
            "oz" if kind == "METAL" else "units"
        sl_line = f"🛑 Stop Loss: {signal.stop_loss_price} (-{signal.stop_loss_pips:g})"
        tp1_line = (
            f"🎯 Take Profit 1: {signal.take_profit_1_price} "
            f"(+{signal.take_profit_1_pips:g} | R:R 1:{signal.take_profit_1_rr:g})"
        )
        tp2_line = (
            f"🎯 Take Profit 2: {signal.take_profit_2_price} "
            f"(+{signal.take_profit_2_pips:g} | R:R 1:{signal.take_profit_2_rr:g})"
        )
        size_line = (
            f"📦 Suggested Risk: {signal.risk.lot_size:g} {unit_label} "
            f"({signal.risk.risk_percent:g}% Risk on ${signal.risk.account_size_usd:,.0f})"
        )

    return "\n".join(
        [
            f"📊 SCAN REPORT: {display_pair(symbol)}",
            "----------------------------------",
            f"🔍 Market Regime: {regime_result.regime.value} ({tf_tag})",
            f"⚡ Strategy Match: {strategy_label}",
            f"🎯 Recommendation: TAKE {signal.direction.value} TRADE {direction_emoji}",
            "",
            f"📈 Entry: {signal.entry_price}",
            sl_line,
            tp1_line,
            tp2_line,
            size_line,
            "----------------------------------",
            f"💡 Indicators: ADX = {regime_result.metrics.adx:.1f} | "
            f"ATR = {regime_result.metrics.atr:.5f} | RSI = {rsi_val:.0f}",
        ]
    )


def _format_no_trade_report(
    symbol: str, primary_tf: str, scanned_tfs: list[str], regime_result: RegimeResult
) -> str:
    tf_tag = "/".join(tf.upper() for tf in scanned_tfs) or primary_tf.upper()
    reason = _NO_TRADE_REASONS.get(
        regime_result.regime, "No active entry trigger matches the current market condition."
    )
    return "\n".join(
        [
            f"📊 SCAN REPORT: {display_pair(symbol)}",
            "----------------------------------",
            f"🔍 Market Regime: {regime_result.regime.value} ({tf_tag})",
            "⚡ Strategy Match: None",
            "⚠️ Recommendation: DO NOT TRADE 🔴",
            "",
            f"💡 Reason: {reason}",
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
            f"📊 SCAN REPORT: {display_pair(symbol)}\n"
            "----------------------------------\n"
            f"⚠️ Couldn't fetch market data for {symbol} ({fetch_ticker}). "
            "It may not be a valid/tradable ticker, or the data source is temporarily unavailable."
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
        await update.message.reply_text(f"Forex Scanner Bot online.\n{_USAGE}")


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
        await update.message.reply_text("The 3-hour auto-scan watchlist is empty. Add one with /add <symbol>.")
        return
    lines = [f"📋 Auto-scan watchlist ({len(symbols)} symbols, every {settings.auto_scan_interval_seconds // 3600}h):"]
    lines.extend(f"• {display_pair(s)}" for s in symbols)
    await update.message.reply_text("\n".join(lines))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return
    raw = " ".join(context.args) if context.args else ""
    symbol, was_new = watchlist_store.add(raw)
    if symbol is None:
        await update.message.reply_text(f"'{raw}' doesn't look like a valid symbol.\n{_USAGE}")
    elif was_new:
        await update.message.reply_text(f"✅ Added {display_pair(symbol)} to the 3-hour auto-scan watchlist.")
    else:
        await update.message.reply_text(f"{display_pair(symbol)} is already on the watchlist.")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return
    raw = " ".join(context.args) if context.args else ""
    symbol, was_removed = watchlist_store.remove(raw)
    if symbol is None:
        await update.message.reply_text(f"'{raw}' doesn't look like a valid symbol.\n{_USAGE}")
    elif was_removed:
        await update.message.reply_text(f"🗑️ Removed {display_pair(symbol)} from the 3-hour auto-scan watchlist.")
    else:
        await update.message.reply_text(f"{display_pair(symbol)} wasn't on the watchlist.")


def _format_paper_status(settings: Settings) -> str:
    if not settings.enable_paper_trading:
        return "Paper trading is disabled (ENABLE_PAPER_TRADING=false)."
    try:
        summary = oanda_client.get_account_summary(settings)
        trades = oanda_client.get_open_trades(settings)
    except (PaperTradingConfigError, PaperTradingRequestError) as exc:
        return f"⚠️ Couldn't reach OANDA paper account: {exc}"

    account = summary.get("account", {})
    lines = [
        "📄 Paper Trading Status (OANDA practice)",
        f"Balance: {account.get('balance')} {account.get('currency', '')}",
        f"Unrealized P&L: {account.get('unrealizedPL')}",
        f"Open trades: {len(trades)}",
    ]
    for t in trades[:10]:
        lines.append(f"  • {t.get('instrument')} {t.get('currentUnits')} @ {t.get('price')}")
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
    change_lines = [f"{key}: {baseline.get(key)} -> {new_val}" for key, new_val in candidate.param_overrides.items()]
    changes_text = "; ".join(change_lines) if change_lines else "no changes from baseline"

    pf = r.profit_factor
    pf_display = f"{pf:.2f}" if isinstance(pf, float) and pf != float("inf") else "∞"

    if candidate.passed:
        status_line = f"✅ Status: PASSED acceptance criteria — reply /apply {candidate.proposal_id} to update the live strategy."
    else:
        status_line = (
            f"❌ Status: Did not pass acceptance criteria (needs Sharpe>{evolver.ACCEPTANCE_MIN_SHARPE}, "
            f"PF>{evolver.ACCEPTANCE_MIN_PROFIT_FACTOR}, MaxDD<{evolver.ACCEPTANCE_MAX_DRAWDOWN_PCT}%) — not applied."
        )

    return "\n".join(
        [
            "🧪 STRATEGY EVOLUTION REPORT",
            "----------------------------------",
            f"Strategy: {evolver.STRATEGY_DISPLAY_LABELS.get(strategy_name, strategy_name)}",
            f"Asset: {display_pair(symbol)} ({timeframe.upper()})",
            "",
            f"📊 Best of {tested_count} Backtest Result{'s' if tested_count != 1 else ''} "
            f"({r.start.date()} to {r.end.date()}):",
            f"• Win Rate: {r.win_rate_pct:.1f}%",
            f"• Profit Factor: {pf_display}",
            f"• Sharpe Ratio: {r.sharpe_ratio:.2f}",
            f"• Max Drawdown: -{r.max_drawdown_pct:.1f}%",
            f"• Total Trades: {r.total_trades}",
            "",
            status_line,
            "----------------------------------",
            f"💡 Changes Tested: {changes_text}",
            f"Proposal ID: {candidate.proposal_id}",
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
        return f"⚠️ Evolution run failed for {display_pair(symbol)} ({timeframe.upper()}) — check the logs."

    if not candidates:
        return (
            f"🧪 STRATEGY EVOLUTION REPORT\n----------------------------------\n"
            f"Strategy: {evolver.STRATEGY_DISPLAY_LABELS.get(strategy_name, strategy_name)}\n"
            f"Asset: {display_pair(symbol)} ({timeframe.upper()})\n\n"
            "⚠️ No backtest results — insufficient historical data for this symbol/timeframe."
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
            "Usage: /evolve <strategy> <symbol> [timeframe]\n"
            f"Strategies: {', '.join(sorted(evolver.PARAM_CLASSES))}\n"
            "Example: /evolve trend_pullback EURUSD 1h\n"
            "(Uses a built-in parameter grid — for a custom grid use the "
            "strategy_evolver.py CLI directly.)"
        )
        return

    strategy_name = args[0].lower()
    if strategy_name not in evolver.PARAM_CLASSES:
        await update.message.reply_text(
            f"Unknown strategy '{args[0]}'. Choices: {', '.join(sorted(evolver.PARAM_CLASSES))}"
        )
        return

    symbol = sanitize_symbol(args[1])
    if symbol is None:
        await update.message.reply_text(f"'{args[1]}' doesn't look like a valid symbol.")
        return

    timeframe = args[2].lower() if len(args) > 2 else settings.telegram_primary_timeframe

    grid = evolver.DEFAULT_GRIDS.get(strategy_name, {})
    combo_count = 1
    for values in grid.values():
        combo_count *= len(values)

    await update.message.reply_text(
        f"🧪 Running strategy evolution: {evolver.STRATEGY_DISPLAY_LABELS.get(strategy_name, strategy_name)} "
        f"on {display_pair(symbol)} ({timeframe.upper()}), {combo_count} parameter combinations "
        f"over 6 months. This can take a couple of minutes..."
    )

    report = await asyncio.to_thread(run_evolution, strategy_name, symbol, timeframe, settings)
    await update.message.reply_text(report)


async def cmd_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not _is_authorized(update, settings) or not update.message:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /apply <proposal_id> (from a /evolve report)")
        return

    proposal_id = args[0]
    proposal = evolver.load_proposal(proposal_id)
    if proposal is None:
        await update.message.reply_text(f"No proposal found with ID {proposal_id}.")
        return

    applied = await asyncio.to_thread(evolver.apply_proposal, proposal_id)
    if applied:
        await update.message.reply_text(
            f"✅ Applied proposal {proposal_id} to {evolver.STRATEGY_DISPLAY_LABELS.get(proposal['strategy_name'], proposal['strategy_name'])}. "
            "Live signals will use the new parameters starting with the next scan. "
            "Previous parameters were backed up to data/strategy_params_backups/."
        )
    else:
        await update.message.reply_text(
            f"❌ Could not apply proposal {proposal_id} — it did not pass the acceptance criteria."
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
        f"Unrecognized command: {update.message.text}\n{_USAGE}"
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
