"""Orchestrator — entry point.

Wires the pipeline: News Filter -> Scanner 1 (regime) -> Scanner 2 (strategy/signal) ->
Notifier, run every `AUTO_SCAN_INTERVAL_SECONDS` (default 1 hour) via
APScheduler over the dynamic watchlist (`src.watchlist_store`), while also
monitoring high-impact economic news every minute so the bot pauses during
news and immediately resumes and alerts the channel once the news window ends.
"""
from __future__ import annotations

import asyncio
import logging
import threading

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from config import Settings, get_settings
from src.news_filter import check_news_status
from src.notifier import (
    send_alert,
    send_news_pause_alert,
    send_news_resume_alert,
    send_no_signal_alert,
)
from src.paper_executor import execute_paper_trade
from src.scanner_1_regime import scan_symbols_with_data
from src.scanner_2_strategy import generate_signal
from src.telegram_bot import build_telegram_application
from src.tradingview_bridge import app as webhook_app
from src.watchlist_store import watchlist_store

logger = logging.getLogger(__name__)

_state_lock = threading.Lock()
_is_paused_for_news: bool = False
_scan_deferred_by_news: bool = False


def monitor_news_state(settings: Settings | None = None) -> bool:
    """يفحص حالة الأخبار كل دقيقة؛ يرسل تنبيه التوقف عند بدء الخبر،
    ويرسل تنبيه العودة الفورية للعمل فور انتهاء الخبر مباشرة."""
    global _is_paused_for_news, _scan_deferred_by_news
    settings = settings or get_settings()

    news_status = check_news_status(settings)
    should_run_deferred_scan = False

    with _state_lock:
        if news_status.is_paused:
            if not _is_paused_for_news:
                _is_paused_for_news = True
                logger.warning("pausing bot due to high-impact news: %s", news_status.details)
                send_news_pause_alert(news_status.details, settings)
            return True
        else:
            if _is_paused_for_news:
                _is_paused_for_news = False
                logger.info("high-impact news window ended — resuming bot operations immediately")
                send_news_resume_alert(settings)
                if _scan_deferred_by_news:
                    _scan_deferred_by_news = False
                    should_run_deferred_scan = True

    if should_run_deferred_scan:
        logger.info("running deferred scan cycle immediately after news clearance")
        run_scan_cycle(settings)

    return False


def run_scan_cycle(settings: Settings | None = None) -> None:
    global _scan_deferred_by_news
    settings = settings or get_settings()

    # 1. التحقق من فلتر الأخبار قبل بدء الفحص
    if monitor_news_state(settings):
        with _state_lock:
            _scan_deferred_by_news = True
        logger.info("auto-scan cycle skipped because high-impact news is active")
        return

    symbols = watchlist_store.get_all()
    logger.info(
        "starting auto-scan cycle: %d watchlist symbols x %d timeframes",
        len(symbols), len(settings.timeframe_list),
    )

    regime_data = scan_symbols_with_data(symbols, settings)
    signals_sent = 0
    suppressed_low_confidence = 0

    for regime_result, df in regime_data:
        signal = generate_signal(regime_result, df, settings)
        if signal is None:
            continue
        if regime_result.confidence < settings.min_signal_confidence:
            suppressed_low_confidence += 1
            logger.info(
                "suppressing low-confidence %s signal for %s/%s (confidence=%.2f < %.2f)",
                signal.direction.value, regime_result.symbol, regime_result.timeframe,
                regime_result.confidence, settings.min_signal_confidence,
            )
            continue
        send_alert(signal, settings)
        signals_sent += 1
        execute_paper_trade(signal, settings)  # no-op unless ENABLE_PAPER_TRADING=true

    logger.info(
        "auto-scan cycle complete: %d/%d (symbol,timeframe) pairs returned data, "
        "%d signals dispatched, %d suppressed for low confidence",
        len(regime_data), len(symbols) * len(settings.timeframe_list),
        signals_sent, suppressed_low_confidence,
    )

    # 2. إرسال تنبيه للقناة في حال وجود تذبذب وعدم توفر أي توصية عالية الدقة خلال هذه الساعة
    if signals_sent == 0 and len(regime_data) > 0 and getattr(settings, "notify_no_signals", True):
        logger.info("no high-confidence signals found in this cycle — sending market chop/no-signal alert")
        send_no_signal_alert(len(regime_data), settings)


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    # مهمة الفحص الدوري كل ساعة (3600 ثانية)
    scheduler.add_job(
        run_scan_cycle,
        "interval",
        seconds=settings.auto_scan_interval_seconds,
        id="forex_auto_scan_cycle",
        kwargs={"settings": settings},
        max_instances=1,
        coalesce=True,
    )
    # مهمة مراقبة الأخبار كل 60 ثانية لضمان التوقف الفوري والعودة الفورية بعد انتهاء الخبر
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


async def _run_webhook_and_telegram(settings: Settings) -> None:
    config = uvicorn.Config(
        webhook_app,
        host=settings.webhook_host,
        port=settings.service_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    telegram_app = None
    if settings.telegram_bot_token:
        telegram_app = build_telegram_application(settings)
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        logger.info("Telegram on-demand bot polling started")
    else:
        logger.info("TELEGRAM_BOT_TOKEN not set — on-demand Telegram bot disabled")

    try:
        await server.serve()
    finally:
        if telegram_app is not None:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
