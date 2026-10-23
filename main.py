"""Orchestrator — entry point.

Wires the pipeline: Scanner 1 (regime) -> Scanner 2 (strategy/signal) ->
Notifier, run every `AUTO_SCAN_INTERVAL_SECONDS` (default 3 hours) via
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
`MIN_SIGNAL_CONFIDENCE` is suppressed, so a 3-hour tick over a growing
watchlist doesn't turn into spam. If `ENABLE_PAPER_TRADING=true`, each
dispatched signal is also mirrored to an OANDA practice account
(`src.paper_executor`) — opt-in, disabled by default.

This module does no indicator/regime/strategy math itself — it only calls
the public entry points each of those modules expose.
"""
from __future__ import annotations

import asyncio
import logging

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
