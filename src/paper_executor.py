"""Bridges a live `TradeSignal` to a simulated order on an OANDA practice
account.

Opt-in via `ENABLE_PAPER_TRADING` (default `false`) — existing alert-only
behavior is completely unaffected unless explicitly turned on. Only wired
into the 3-hour auto-scan path (`main.py`), never on-demand `/scan`
requests, so poking the bot with a curious ticker never opens a demo
position by accident.
"""
from __future__ import annotations

import logging

from config import Settings, get_settings
from src import oanda_client, risk
from src.models import TradeSignal
from src.symbol_resolver import instrument_type

logger = logging.getLogger(__name__)


def execute_paper_trade(signal: TradeSignal, settings: Settings | None = None) -> bool:
    """Returns True if an order was placed and filled, False otherwise
    (disabled, unsupported instrument, zero size, or a request failure —
    never raises, so a paper-trading hiccup can't take down the scan loop)."""
    settings = settings or get_settings()
    if not settings.enable_paper_trading:
        return False

    if instrument_type(signal.symbol) != "FX":
        logger.info(
            "skipping paper trade for %s: only FX instruments are supported by the OANDA bridge", signal.symbol
        )
        return False

    units = round(signal.risk.lot_size * risk.STANDARD_LOT_UNITS)
    if units <= 0:
        logger.warning("skipping paper trade for %s: computed 0 units", signal.symbol)
        return False

    try:
        result = oanda_client.place_market_order(
            signal.symbol, signal.direction, units,
            signal.stop_loss_price, signal.take_profit_1_price, settings,
        )
    except Exception:
        logger.exception("paper trade execution failed for %s", signal.symbol)
        return False

    logger.info(
        "paper trade for %s %s: status=%s trade_id=%s units=%d",
        signal.symbol, signal.direction.value, result.status, result.trade_id, units,
    )
    return result.status == "FILLED"
