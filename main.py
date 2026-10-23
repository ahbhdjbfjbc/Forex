"""Orchestrator — entry point."""
from __future__ import annotations

import asyncio
import logging
import time

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from config import Settings, get_settings
from src.notifier import send_alert
from src.paper_executor import execute_paper_trade
from src.scanner_1_regime import scan_symbols_with_data
from src.scanner_2_strategy import generate_signal
from src.telegram_bot import build_telegram_application
from src.tradingview_bridge import app as webhook_app
from src.watchlist_store import watchlist_store

logger = logging.getLogger(__name__)

# ذاكرة لحفظ وقت آخر توصية لكل زوج وفريم لمنع التكرار (4 ساعات = 14400 ثانية)
_LAST_SIGNAL_SENT: dict[tuple[str, str, str], float] = {}
SIGNAL_COOLDOWN_SECONDS = 4 * 60 * 60


def _can_send_signal(symbol: str, timeframe: str, direction: str) -> bool:
    key = (symbol, timeframe, direction)
    now = time.time()
    last_time = _LAST_SIGNAL_SENT.get(key)
    if last_time is not None and (now - last_time) < SIGNAL_COOLDOWN_SECONDS:
        return False
    _LAST_SIGNAL_SENT[key] = now
    return True


def run_scan_cycle(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    symbols = watchlist_store.get_all()
    logger.info(
        "starting auto-scan cycle: %d watchlist symbols x %d timeframes",
        len(symbols), len(settings.timeframe_list),
    )

    regime_data = scan_symbols_with_data(symbols, settings)
    signals_sent = 0
    suppressed_low_confidence = 0
    suppressed_duplicate = 0

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

        # فحص عدم تكرار التوصية خلال نفس الشمعة
        if not _can_send_signal(regime_result.symbol, regime_result.timeframe, signal.direction.value):
            suppressed_duplicate += 1
            logger.info(
                "suppressing duplicate %s signal for %s/%s (within cooldown)",
                signal.direction.value, regime_result.symbol, regime_result.timeframe,
            )
            continue

        send_alert(signal, settings)
        signals_sent += 1
        execute_paper_trade(signal, settings)

    logger.info(
        "auto-scan cycle complete: %d/%d (symbol,timeframe) pairs returned data, "
        "%d signals dispatched, %d low confidence, %d duplicates suppressed",
        len(regime_data), len(symbols) * len(settings.timeframe_list),
        signals_sent, suppressed_low_confidence, suppressed_duplicate,
    )


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    # التأكد من أن وقت الفحص لا يقل عن 15 دقيقة (900 ثانية) حتى لو تم تغييره بالخطأ في الإعدادات
    interval = max(settings.auto_scan_interval_seconds, 900)
    scheduler.add_job(
        run_scan_cycle,
        "interval",
        seconds=interval,
        id="forex_auto_scan_cycle",
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
    logger.info("scheduler started: auto-scanning every %ds", max(settings.auto_scan_interval_seconds, 900))

    try:
        asyncio.run(_run_webhook_and_telegram(settings))
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
