"""Last check before any recommendation reaches the Telegram channel.

`notifier.send_alert` is the single publication chokepoint used by the
hourly auto-scan, the admin `/scan_all` command, the manual publish button
and the TradingView webhook. Calling this gate there guarantees that every
path applies the same rules:

1. high-impact news for the instrument's own currencies (fails closed when
   the calendar is unavailable — the same policy as the original filter);
2. FX/metals market hours (no publishing during the weekend close);
3. multi-timeframe alignment — the next higher timeframe must not be in a
   strong opposite trend or have opposite structure with price on the wrong
   side of its EMA50 (fails closed when that data cannot be fetched);
4. gold only: a strong same-day US Dollar Index move against the trade.
   DXY is optional context; when it cannot be fetched the gate allows the
   trade and says so in the log instead of inventing a value.

Each decision returns a human-readable Arabic reason that is logged and
surfaced to the admin.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
import time

from config import Settings, get_settings
from src.models import MarketRegime, StrategyType, TradeDirection, TradeSignal
from src.symbol_resolver import instrument_type, sanitize_symbol, to_yfinance_ticker

logger = logging.getLogger(__name__)

HIGHER_TIMEFRAME = {
    "1m": "15m", "5m": "1h", "15m": "1h", "30m": "4h",
    "1h": "4h", "60": "4h", "2h": "1d", "4h": "1d", "240": "1d",
    "15": "1h", "30": "4h",
}


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""


def fx_market_open(now: datetime | None = None) -> bool:
    """Spot FX/metals trade from Sunday ~22:00 UTC to Friday ~21:00 UTC."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    weekday, hour = now.weekday(), now.hour
    if weekday == 5:
        return False
    if weekday == 4 and hour >= 21:
        return False
    if weekday == 6 and hour < 22:
        return False
    return True


def _canonical(symbol: str) -> str:
    raw = str(symbol).upper()
    if raw in {"GC=F"}:
        return "XAUUSD"
    if raw in {"SI=F"}:
        return "XAGUSD"
    return sanitize_symbol(raw.replace("=X", "")) or raw


def _check_news(signal: TradeSignal, settings: Settings) -> GateDecision:
    if not getattr(settings, "enable_news_filter", True):
        return GateDecision(True)
    from src.news_filter import check_news_for_symbol

    status = check_news_for_symbol(signal.symbol, settings)
    if status.is_paused:
        return GateDecision(False, f"فلتر الأخبار: {status.details or 'خبر قوي قريب'}")
    return GateDecision(True)


def _check_market_hours(signal: TradeSignal, settings: Settings, now: datetime | None) -> GateDecision:
    if not getattr(settings, "enable_market_hours_filter", True):
        return GateDecision(True)
    if instrument_type(signal.symbol) == "CRYPTO":
        return GateDecision(True)
    if not fx_market_open(now):
        return GateDecision(False, "سوق الفوركس/الذهب مغلق (عطلة نهاية الأسبوع)")
    return GateDecision(True)


def _check_mtf(signal: TradeSignal, settings: Settings) -> GateDecision:
    if not getattr(settings, "enable_mtf_filter", True):
        return GateDecision(True)
    timeframe = str(signal.timeframe).lower()
    higher = HIGHER_TIMEFRAME.get(timeframe)
    if higher is None:
        return GateDecision(True)  # 1d and above: no higher frame is checked
    from src.indicators import ema
    from src.market_structure import BEARISH, BULLISH, analyze_structure
    from src.scanner_1_regime import scan_regime_with_data

    canonical = _canonical(signal.symbol)
    fetched = scan_regime_with_data(canonical, higher, settings, fetch_symbol=to_yfinance_ticker(canonical))
    if fetched is None:
        return GateDecision(False, f"تعذر جلب الإطار الأعلى {higher} للتحقق من توافق الاتجاه")
    result, frame = fetched
    buy = signal.direction == TradeDirection.BUY
    opposite_regime = MarketRegime.STRONG_TREND_BEARISH if buy else MarketRegime.STRONG_TREND_BULLISH
    if result.regime == opposite_regime:
        return GateDecision(False, f"الإطار الأعلى {higher} في اتجاه قوي معاكس")
    if signal.strategy == StrategyType.MEAN_REVERSION_FADE:
        return GateDecision(True)
    structure = analyze_structure(frame, float(result.metrics.atr))
    if structure is None:
        return GateDecision(True)
    ema50 = float(ema(frame["Close"], 50).iloc[-1])
    close = float(frame["Close"].iloc[-1])
    if buy and structure.trend == BEARISH and close < ema50:
        return GateDecision(False, f"هيكل الإطار {higher} هابط والسعر تحت EMA50")
    if not buy and structure.trend == BULLISH and close > ema50:
        return GateDecision(False, f"هيكل الإطار {higher} صاعد والسعر فوق EMA50")
    return GateDecision(True)


_DXY_CACHE: dict[str, tuple[float, float | None]] = {}
_DXY_LOCK = threading.Lock()
_DXY_TTL_SECONDS = 600
DXY_MOVE_THRESHOLD_PCT = 0.35


def _fetch_dxy_change_pct() -> float | None:
    """Percent change of DXY over the last ~24 hourly bars, or None if unavailable."""
    with _DXY_LOCK:
        cached = _DXY_CACHE.get("dxy")
        if cached and time.monotonic() - cached[0] < _DXY_TTL_SECONDS:
            return cached[1]
    value = None
    try:
        import yfinance as yf

        frame = yf.Ticker("DX-Y.NYB").history(period="5d", interval="1h")
        closes = frame["Close"].dropna()
        if len(closes) >= 25:
            value = float((closes.iloc[-1] / closes.iloc[-25] - 1.0) * 100.0)
    except Exception as exc:  # optional context only
        logger.info("DXY context unavailable (%s)", type(exc).__name__)
    with _DXY_LOCK:
        _DXY_CACHE["dxy"] = (time.monotonic(), value)
    return value


def _check_dxy(signal: TradeSignal, settings: Settings) -> GateDecision:
    if not getattr(settings, "enable_dxy_filter", True) or _canonical(signal.symbol) != "XAUUSD":
        return GateDecision(True)
    change = _fetch_dxy_change_pct()
    if change is None:
        logger.info("gold DXY filter skipped: DXY data unavailable")
        return GateDecision(True)
    buy = signal.direction == TradeDirection.BUY
    if buy and change >= DXY_MOVE_THRESHOLD_PCT:
        return GateDecision(False, f"مؤشر الدولار DXY صاعد بقوة ({change:+.2f}% خلال 24 ساعة) ضد شراء الذهب")
    if not buy and change <= -DXY_MOVE_THRESHOLD_PCT:
        return GateDecision(False, f"مؤشر الدولار DXY هابط بقوة ({change:+.2f}% خلال 24 ساعة) ضد بيع الذهب")
    return GateDecision(True)


def check_publish_allowed(
    signal: TradeSignal, settings: Settings | None = None, *, now: datetime | None = None
) -> GateDecision:
    """Run every publish rule; the first failing rule wins. Errors fail closed."""
    settings = settings or get_settings()
    checks = (
        lambda: _check_market_hours(signal, settings, now),
        lambda: _check_news(signal, settings),
        lambda: _check_mtf(signal, settings),
        lambda: _check_dxy(signal, settings),
    )
    for check in checks:
        try:
            decision = check()
        except Exception:
            logger.exception("publish gate check failed for %s/%s", signal.symbol, signal.timeframe)
            return GateDecision(False, "تعذر إكمال فحوصات النشر؛ تم الرفض احترازياً")
        if not decision.allowed:
            logger.info("publish gate blocked %s/%s: %s", signal.symbol, signal.timeframe, decision.reason)
            return decision
    return GateDecision(True)
