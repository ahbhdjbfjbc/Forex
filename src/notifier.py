"""Alert Dispatcher — formats and sends trade signals to Telegram/Discord/desktop.

Owns message formatting and delivery only. Never computes trading logic —
it receives a finished `TradeSignal` and turns it into text + HTTP calls.
Each channel is dispatched independently and failures are caught per-channel
so, e.g., a Telegram outage never blocks the Discord send.
"""
from __future__ import annotations

import logging
import platform
import subprocess

import httpx

from config import Settings, get_settings
from src.models import MarketRegime, StrategyType, TradeDirection, TradeSignal
from src.symbol_resolver import instrument_type

logger = logging.getLogger(__name__)

# Public (imported by src/telegram_bot.py for the on-demand /scan report so
# both message formats stay in sync instead of duplicating these labels).
DIRECTION_EMOJI = {TradeDirection.BUY: "🟢", TradeDirection.SELL: "🔴"}

REGIME_LABELS = {
    MarketRegime.STRONG_TREND_BULLISH: "Strong Trend Bullish",
    MarketRegime.STRONG_TREND_BEARISH: "Strong Trend Bearish",
    MarketRegime.RANGING_CONSOLIDATION: "Ranging Consolidation",
    MarketRegime.HIGH_VOLATILITY_EXPANSION: "High Volatility Expansion",
    MarketRegime.LOW_LIQUIDITY_CHOP: "Low Liquidity Chop",
}

STRATEGY_LABELS = {
    StrategyType.TREND_PULLBACK: "Trend Retracement (EMA 20 Touch)",
    StrategyType.MEAN_REVERSION_FADE: "Mean Reversion Fade (BB + RSI)",
    StrategyType.VOLATILITY_BREAKOUT: "Volatility Breakout (Donchian/Keltner)",
    StrategyType.NO_TRADE: "No Trade",
}


def format_pair(symbol: str) -> str:
    clean = symbol.upper().replace("=X", "").replace("/", "")
    if len(clean) == 6:
        return f"{clean[:3]}/{clean[3:]}"
    return clean


def format_message(signal: TradeSignal) -> str:
    regime_label = REGIME_LABELS.get(signal.regime, signal.regime.value)
    strategy_label = STRATEGY_LABELS.get(signal.strategy, signal.strategy.value)
    direction_emoji = DIRECTION_EMOJI.get(signal.direction, "")
    tf_label = signal.timeframe.upper()

    if instrument_type(signal.symbol) == "FX":
        sl_text = f"Stop Loss: {signal.stop_loss_price} (-{signal.stop_loss_pips:g} pips)"
        tp1_text = (f"Take Profit 1: {signal.take_profit_1_price} "
                    f"(+{signal.take_profit_1_pips:g} pips, R:R 1:{signal.take_profit_1_rr:g})")
        tp2_text = (f"Take Profit 2: {signal.take_profit_2_price} "
                    f"(+{signal.take_profit_2_pips:g} pips, R:R 1:{signal.take_profit_2_rr:g})")
        size_text = f"Recommended Lot: {signal.risk.lot_size:g} standard lots"
    else:
        unit = "BTC" if signal.symbol.upper().startswith("BTC") else "ETH" if signal.symbol.upper().startswith("ETH") else "oz" if instrument_type(signal.symbol) == "METAL" else "units"
        sl_text = f"Stop Loss: {signal.stop_loss_price} (price distance {signal.stop_loss_pips:g})"
        tp1_text = (f"Take Profit 1: {signal.take_profit_1_price} "
                    f"(price distance {signal.take_profit_1_pips:g}, R:R 1:{signal.take_profit_1_rr:g})")
        tp2_text = (f"Take Profit 2: {signal.take_profit_2_price} "
                    f"(price distance {signal.take_profit_2_pips:g}, R:R 1:{signal.take_profit_2_rr:g})")
        size_text = f"Indicative Size: {signal.risk.lot_size:g} {unit} (broker contract size may differ)"

    lines = [
        "🚨 TRADE SIGNAL TRIGGERED 🚨",
        f"Pair: {format_pair(signal.symbol)}",
        f"Direction: {signal.direction.value} {direction_emoji}",
        f"Regime: {regime_label} ({tf_label})",
        f"Strategy: {strategy_label}",
        "------------------------------",
        f"Entry: {signal.entry_price}",
        sl_text,
        tp1_text,
        tp2_text,
        size_text + f" (risk setting: {signal.risk.risk_percent:g}% of ${signal.risk.account_size_usd:,.0f})",
        "------------------------------",
        f"Time: {signal.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC",
    ]
    return "\n".join(lines)


def send_telegram(message: str, settings: Settings) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("telegram not configured, skipping")
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        resp = httpx.post(
            url, json={"chat_id": settings.telegram_chat_id, "text": message}, timeout=10.0
        )
        if resp.status_code == 429:
            # Do not log response bodies or request URLs; the bot token is part
            # of Telegram's URL path and may otherwise appear in exceptions.
            logger.warning("telegram rate-limited (HTTP %s)", resp.status_code)
            return False
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        # HTTPX exceptions can include the full request URL, which contains the
        # Telegram bot token. Log only the exception class, never its text.
        logger.error("telegram send failed (%s)", type(exc).__name__)
        return False


def send_discord(message: str, settings: Settings) -> bool:
    if not settings.discord_webhook_url:
        logger.debug("discord not configured, skipping")
        return False
    try:
        resp = httpx.post(settings.discord_webhook_url, json={"content": message}, timeout=10.0)
        if resp.status_code == 429:
            # Discord webhook URLs contain a credential in the path.
            logger.warning("discord rate-limited (HTTP %s)", resp.status_code)
            return False
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        # Exception text may contain the webhook URL/token; avoid logging it.
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
            title = "Forex Signal"
            safe_message = _escape_applescript_string(message[:200])
            safe_title = _escape_applescript_string(title)
            script = f'display notification "{safe_message}" with title "{safe_title}"'
            subprocess.run(["osascript", "-e", script], check=True, timeout=5)
            return True
        logger.debug("desktop notifications only implemented for macOS in this build")
        return False
    except Exception as exc:
        # Do not echo arbitrary signal text or command output into logs.
        logger.error("desktop notification failed (%s)", type(exc).__name__)
        return False


def send_alert(signal: TradeSignal, settings: Settings | None = None) -> dict[str, bool]:
    """Format the signal once and dispatch to every channel independently.
    Returns a per-channel success map; never raises."""
    settings = settings or get_settings()
    message = format_message(signal)

    results: dict[str, bool] = {}
    channels = (
        ("telegram", send_telegram),
        ("discord", send_discord),
        ("desktop", send_desktop),
    )
    for name, dispatcher in channels:
        try:
            results[name] = bool(dispatcher(message, settings))
        except Exception as exc:
            # A malformed URL or unexpected adapter exception must not prevent
            # later channels from receiving the signal. Exception text can
            # include credential-bearing URLs, so log only the type.
            logger.error("%s notification failed (%s)", name, type(exc).__name__)
            results[name] = False
    if not any(results.values()):
        logger.warning(
            "no notification channel is configured/succeeded for %s %s signal",
            signal.symbol, signal.direction.value,
        )
    return results
