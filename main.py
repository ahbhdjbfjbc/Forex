"""Orchestrator — entry point.

Wires the pipeline: Scanner 1 (regime) -> Scanner 2 (strategy/signal) ->
Notifier, run every `AUTO_SCAN_INTERVAL_SECONDS` (default 1 hour) via
APScheduler over the dynamic watchlist (`src.watchlist_store` — seeded with
defaults, grown by `/add` and by successful on-demand `/scan` requests),
while also serving:
  - the TradingView webhook bridge (FastAPI/uvicorn), for externally
    triggered Pine Script alerts, and
  - the on-demand Telegram bot (src/telegram_bot.py), for typing a ticker
    and getting an immediate scan, or managing the watchlist.

Both the webhook server and the Telegram bot are asyncio-based, so they run
concurrently in a single event loop; APScheduler's BackgroundScheduler runs
the periodic scan loop on its own thread independently of that loop.

Only high-confidence, actively-triggered setups get pushed — anything with
no active entry trigger (Scanner 2 already returns None for those) or below
`MIN_SIGNAL_CONFIDENCE` is suppressed, so a periodic tick over a growing
watchlist doesn't turn into spam. If `ENABLE_PAPER_TRADING=true`, each
dispatched signal is also mirrored to an OANDA practice account
(`src.paper_executor`) — opt-in, disabled by default.

This module does no indicator/regime/strategy math itself — it only calls
the public entry points each of those modules expose.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import threading

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from config import Settings, get_settings
from src.notifier import (
    flush_pending_signal_outcomes,
    send_alert,
    send_news_pause_alert,
    send_news_resume_alert,
    send_admin_message,
    send_no_signal_alert,
    send_signal_outcome,
)
from src.data_provider import OHLCVRequest, fetch_ohlcv
from src.publish_gate import fx_market_open
from src.symbol_resolver import instrument_type, to_yfinance_ticker
from src.paper_executor import execute_paper_trade
from src.scanner_1_regime import scan_symbols_with_data
from src.scanner_2_strategy import generate_signal
from src.signal_lifecycle import normalize_symbol, signal_lifecycle
from src.telegram_bot import build_telegram_application, publish_telegram_command_menu
from src.tradingview_bridge import app as webhook_app, tradingview_heartbeat_ok
from src.watchlist_store import watchlist_store
from src.news_filter import NewsStatus, check_news_status
from src.system_health import report_health

logger = logging.getLogger(__name__)

_state_lock = threading.Lock()
_is_paused_for_news: bool = False
_scan_deferred_by_news: bool = False
_auto_signal_lock = threading.Lock()
_recent_auto_signal_keys: OrderedDict[tuple[str, ...], None] = OrderedDict()
_MAX_RECENT_AUTO_SIGNALS = 4096


def _claim_auto_signal(signal, frame) -> tuple[str, ...] | None:
    """Claim one signal per market candle so periodic scans do not resend it."""
    try:
        bar_identity = str(frame.index[-1])
    except (AttributeError, IndexError, TypeError):
        timestamp = getattr(signal, "timestamp", None)
        bar_identity = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)

    key = (
        str(getattr(signal, "symbol", "")).upper(),
        str(getattr(signal, "timeframe", "")).lower(),
        str(getattr(getattr(signal, "direction", ""), "value", getattr(signal, "direction", ""))),
        str(getattr(getattr(signal, "strategy", ""), "value", getattr(signal, "strategy", ""))),
        bar_identity,
    )
    with _auto_signal_lock:
        if key in _recent_auto_signal_keys:
            return None
        _recent_auto_signal_keys[key] = None
        while len(_recent_auto_signal_keys) > _MAX_RECENT_AUTO_SIGNALS:
            _recent_auto_signal_keys.popitem(last=False)
    return key


_NEWS_STATE_FILE = "news_pause_state.json"


def _save_news_pause_state() -> None:
    """Persist the pause flags so a restart during news still sends the resume alert."""
    try:
        import json
        from src.storage import get_data_dir

        path = get_data_dir() / _NEWS_STATE_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "paused": bool(_is_paused_for_news),
            "deferred": bool(_scan_deferred_by_news),
        }), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.warning("could not persist news pause state (%s)", type(exc).__name__)


def _load_news_pause_state() -> None:
    global _is_paused_for_news, _scan_deferred_by_news
    try:
        import json
        from src.storage import get_data_dir

        path = get_data_dir() / _NEWS_STATE_FILE
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        with _state_lock:
            _is_paused_for_news = bool(data.get("paused"))
            _scan_deferred_by_news = bool(data.get("deferred"))
    except Exception as exc:
        logger.warning("could not load news pause state (%s)", type(exc).__name__)


def monitor_news_state(settings: Settings | None = None) -> bool:
    """يفحص حالة الأخبار كل دقيقة؛ يرسل تنبيه التوقف عند بدء الخبر،
    ويرسل تنبيه العودة الفورية للعمل فور انتهاء الخبر مباشرة."""
    global _is_paused_for_news, _scan_deferred_by_news
    settings = settings or get_settings()

    try:
        news_status = check_news_status(settings)
    except Exception:
        logger.exception("news filter check failed; pausing recommendations until data recovers")
        news_status = NewsStatus(
            is_paused=True,
            details="تعذر التحقق من تقويم الأخبار؛ تم إيقاف التوصيات مؤقتاً احترازياً.",
            data_available=False,
        )

    report_health(
        "economic_calendar",
        bool(news_status.data_available),
        news_status.details,
        settings,
    )

    should_run_deferred_scan = False

    with _state_lock:
        if news_status.is_paused:
            if not _is_paused_for_news:
                _is_paused_for_news = True
                logger.warning("pausing bot due to high-impact news: %s", news_status.details)
                send_news_pause_alert(news_status.details, settings)
                _save_news_pause_state()
            return True
        else:
            if _is_paused_for_news:
                _is_paused_for_news = False
                logger.info("high-impact news window ended — resuming bot operations immediately")
                send_news_resume_alert(settings)
                if _scan_deferred_by_news:
                    _scan_deferred_by_news = False
                    should_run_deferred_scan = True
                _save_news_pause_state()

    if should_run_deferred_scan:
        logger.info("running deferred scan cycle immediately after news clearance")
        run_scan_cycle(settings)

    return False


def run_scan_cycle(settings: Settings | None = None) -> None:
    global _scan_deferred_by_news
    settings = settings or get_settings()

    # التحقق من الأخبار قبل بدء الفحص؛ إذا كان هناك خبر قوي يتوقف مؤقتاً ويعود فور انتهائه
    if monitor_news_state(settings):
        with _state_lock:
            _scan_deferred_by_news = True
        logger.info("auto-scan cycle paused due to active high-impact economic news")
        return

    flush_pending_signal_outcomes(settings)
    scan_symbols = {normalize_symbol(symbol): symbol for symbol in watchlist_store.get_all()}
    for active_symbol in signal_lifecycle.active_symbols():
        scan_symbols.setdefault(normalize_symbol(active_symbol), active_symbol)
    symbols = sorted(scan_symbols.values())
    logger.info(
        "starting auto-scan cycle: %d watchlist symbols x %d timeframes",
        len(symbols), len(settings.timeframe_list),
    )

    try:
        regime_data = scan_symbols_with_data(symbols, settings)
    except Exception as exc:
        logger.exception("market scan failed; no recommendation will be emitted in this cycle")
        report_health(
            "market_data", False,
            f"فشل جلب/تحليل بيانات السوق ({type(exc).__name__})؛ أُوقف هذا المسح.",
            settings,
        )
        return

    fx_open = fx_market_open()
    expected_pairs = {
        (normalize_symbol(symbol), timeframe.lower())
        for symbol in symbols
        for timeframe in settings.timeframe_list
        # During the weekend FX/metals close, stale data is expected and not an outage.
        if fx_open or instrument_type(symbol) == "CRYPTO"
    }
    received_pairs = {
        (normalize_symbol(getattr(result, "symbol", "")), str(getattr(result, "timeframe", "")).lower())
        for result, _frame in regime_data
    }
    market_healthy = (bool(regime_data) or not expected_pairs) and expected_pairs.issubset(received_pairs)
    coverage = f"بيانات حديثة متاحة لـ {len(received_pairs & expected_pairs)}/{len(expected_pairs)} زوج/إطار."
    report_health("market_data", market_healthy, coverage, settings)
    signals_sent = 0
    suppressed_low_confidence = 0

    for regime_result, df in regime_data:
        bar_id = None
        symbol = getattr(regime_result, "symbol", None)
        timeframe = getattr(regime_result, "timeframe", None)
        if symbol and timeframe:
            try:
                bar_id = str(df.index[-1])
                outcome = signal_lifecycle.observe_frame(symbol, timeframe, df)
                if outcome is not None:
                    send_signal_outcome(outcome, settings)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                logger.exception(
                    "could not evaluate active recommendation outcome for %s/%s",
                    symbol, timeframe,
                )

        signal = generate_signal(regime_result, df, settings)
        if signal is None:
            if symbol and timeframe:
                signal_lifecycle.reset_block(symbol, timeframe)
            continue
        if regime_result.confidence < settings.min_signal_confidence:
            suppressed_low_confidence += 1
            logger.info(
                "suppressing low-confidence %s signal for %s/%s (confidence=%.2f < %.2f)",
                signal.direction.value, regime_result.symbol, regime_result.timeframe,
                regime_result.confidence, settings.min_signal_confidence,
            )
            continue
        if _claim_auto_signal(signal, df) is None:
            logger.info(
                "suppressing duplicate auto-scan signal for %s/%s on the same market candle",
                signal.symbol, signal.timeframe,
            )
            continue
        delivery = send_alert(
            signal, settings, source="auto-scan", entry_bar_id=bar_id, market_frame=df
        )
        if delivery.get("duplicate"):
            continue
        if any(value for channel, value in delivery.items() if channel != "duplicate"):
            signals_sent += 1
            execute_paper_trade(signal, settings)  # no-op unless ENABLE_PAPER_TRADING=true

    logger.info(
        "auto-scan cycle complete: %d/%d (symbol,timeframe) pairs returned data, "
        "%d signals dispatched, %d suppressed for low confidence",
        len(regime_data), len(symbols) * len(settings.timeframe_list),
        signals_sent, suppressed_low_confidence,
    )
    flush_pending_signal_outcomes(settings)

    if settings.ai_review_enabled:
        try:
            from src.ai_assistant import maybe_send_periodic_report

            maybe_send_periodic_report(settings)
        except Exception as exc:
            logger.error("optional AI review scheduler failed (%s)", type(exc).__name__)

    # إرسال تنبيه في حال وجود تذبذب وعدم توفر توصيات جيدة خلال هذه الساعة
    if signals_sent == 0 and len(regime_data) > 0 and getattr(settings, "notify_no_signals", False):
        logger.info("no high-confidence signals in this cycle — sending chop/no-signal alert")
        send_no_signal_alert(len(regime_data), settings)


def monitor_open_signals(settings: Settings | None = None) -> int:
    """Follow every open recommendation until TP/SL, independent of the hourly scan.

    Runs every few minutes (and also during news pauses, which only stop new
    publications) so results reach the channel close to when they happen.
    Returns the number of outcomes found in this pass."""
    settings = settings or get_settings()
    found = 0
    try:
        flush_pending_signal_outcomes(settings)
        markets = signal_lifecycle.active_markets()
    except Exception:
        logger.exception("open-signal monitor could not read the lifecycle store")
        return 0
    for symbol, timeframe in markets:
        try:
            frame = fetch_ohlcv(OHLCVRequest(symbol=to_yfinance_ticker(symbol), timeframe=timeframe, min_bars=30))
            if frame is None or frame.empty:
                continue
            outcome = signal_lifecycle.observe_frame(symbol, timeframe, frame)
            if outcome is not None:
                found += 1
                send_signal_outcome(outcome, settings)
        except Exception:
            logger.exception("open-signal monitor failed for %s/%s", symbol, timeframe)
    try:
        ok, detail = tradingview_heartbeat_ok(settings)
        if detail != "disabled":
            report_health("tradingview", ok, detail if not ok else "تنبيهات TradingView تصل بانتظام.", settings)
    except Exception:
        logger.exception("TradingView heartbeat check failed")
    return found


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_scan_cycle,
        "interval",
        seconds=settings.auto_scan_interval_seconds,
        id="forex_auto_scan_cycle",
        kwargs={"settings": settings},
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        monitor_open_signals,
        "interval",
        seconds=max(60, int(getattr(settings, "outcome_check_interval_seconds", 300))),
        id="forex_open_signal_monitor",
        kwargs={"settings": settings},
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        monitor_news_state,
        "interval",
        seconds=60,
        id="forex_news_monitor_cycle",
        kwargs={"settings": settings},
        max_instances=1,
        coalesce=True,
    )
    return scheduler


_conflict_reported = False


def _polling_error_callback(settings: Settings):
    """Explain getUpdates failures instead of silently retrying forever."""

    def _callback(exc: Exception) -> None:
        global _conflict_reported
        try:
            from telegram.error import Conflict, InvalidToken
        except ImportError:  # pragma: no cover - PTB is a hard dependency
            Conflict = InvalidToken = ()  # type: ignore[assignment]
        if Conflict and isinstance(exc, Conflict):
            logger.error(
                "Telegram polling conflict: another process is polling with the same "
                "TELEGRAM_BOT_TOKEN (local PC, second Railway service or old deployment). "
                "Only one instance may run, otherwise commands are answered randomly or not at all."
            )
            if not _conflict_reported:
                _conflict_reported = True
                threading.Thread(
                    target=send_admin_message,
                    args=(
                        "⚠️ تعارض في البوت: توجد نسخة أخرى تعمل بنفس التوكن "
                        "(جهاز محلي أو خدمة ثانية). أوقف النسخة الأخرى حتى تستجيب الأوامر.",
                        settings,
                    ),
                    daemon=True,
                ).start()
            return
        if InvalidToken and isinstance(exc, InvalidToken):
            logger.error("Telegram rejected TELEGRAM_BOT_TOKEN; check the token in the deployment settings")
            return
        logger.warning("Telegram polling error (%s); PTB will retry", type(exc).__name__)

    return _callback


async def _notify_admin_startup(telegram_app, settings: Settings) -> None:
    """Private admin message proving the bot is alive, with channel permissions."""
    try:
        from src.telegram_bot import check_channel_permissions

        report = await check_channel_permissions(telegram_app.bot, settings)
    except Exception as exc:
        report = f"📢 تعذر فحص صلاحيات القناة ({type(exc).__name__})"
    message = (
        "✅ تم تشغيل بوت TONPO Signals وهو يستقبل الأوامر الآن.\n"
        f"{report}\n"
        "أرسل /status أو /results للتحقق."
    )
    await asyncio.to_thread(send_admin_message, message, settings)


async def _start_telegram(settings: Settings):
    """Start Telegram polling once; return the application or None on failure."""
    telegram_app = build_telegram_application(settings)
    try:
        await telegram_app.initialize()
        await publish_telegram_command_menu(telegram_app.bot)
        await telegram_app.start()
        await telegram_app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            # Keep user commands sent while the service was restarting. Dropping
            # the queue made /start and other commands appear to be ignored.
            drop_pending_updates=False,
            error_callback=_polling_error_callback(settings),
        )
    except Exception as exc:
        logger.error(
            "Telegram bot could not start (%s: %s); the webhook keeps running and "
            "the bot start will be retried", type(exc).__name__, str(exc)[:200],
        )
        for step in ("updater.stop", "stop", "shutdown"):
            try:
                target = telegram_app
                for attr in step.split("."):
                    target = getattr(target, attr)
                await target()
            except Exception:
                pass
        return None
    logger.info("Telegram on-demand bot polling started")
    return telegram_app


async def _run_webhook_and_telegram(settings: Settings) -> None:
    config = uvicorn.Config(
        webhook_app,
        host=settings.webhook_host,
        port=settings.service_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    holder: dict[str, object] = {"app": None}
    background: list[asyncio.Task] = []
    if settings.telegram_bot_token:
        holder["app"] = await _start_telegram(settings)

        async def _retry_until_started() -> None:
            delay = 15
            while holder["app"] is None:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
                holder["app"] = await _start_telegram(settings)
            background.append(asyncio.create_task(_notify_admin_startup(holder["app"], settings)))

        if holder["app"] is None:
            background.append(asyncio.create_task(_retry_until_started()))
        else:
            background.append(asyncio.create_task(_notify_admin_startup(holder["app"], settings)))
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set — Telegram command bot is disabled; "
            "set it in the deployment environment to start polling"
        )

    try:
        await server.serve()
    finally:
        for task in background:
            task.cancel()
        telegram_app = holder["app"]
        if telegram_app is not None:
            for step in (telegram_app.updater.stop, telegram_app.stop, telegram_app.shutdown):
                try:
                    await step()
                except Exception as exc:
                    logger.warning("Telegram shutdown step failed (%s)", type(exc).__name__)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _load_news_pause_state()
    scheduler = build_scheduler(settings)
    scheduler.add_job(
        run_scan_cycle,
        "date",
        id="forex_initial_scan_cycle",
        kwargs={"settings": settings},
        max_instances=1,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("scheduler started: auto-scanning every %ds", settings.auto_scan_interval_seconds)

    try:
        asyncio.run(_run_webhook_and_telegram(settings))
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
