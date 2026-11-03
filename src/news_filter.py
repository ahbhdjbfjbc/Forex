"""Economic calendar gate for high-impact FX and metals events.

A failed or expired calendar feed is not interpreted as "no news". When the
filter is enabled, unavailable calendar data pauses signal publication until a
fresh response is available. The service only consumes the provider's public
calendar feed; it does not scrape or automate TradingView.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import threading
from typing import Any

import httpx

from config import Settings, get_settings

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_TTL_SECONDS = 3 * 60 * 60
MAJOR_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}

_cached_events: list[dict[str, Any]] | None = None
_last_fetch_time: datetime | None = None
_fetch_lock = threading.Lock()


@dataclass(frozen=True)
class NewsStatus:
    is_paused: bool
    details: str = ""
    data_available: bool = True


def _fetch_calendar_events() -> list[dict[str, Any]] | None:
    """Return fresh cached/fetched events, or None when the feed is unavailable."""
    global _cached_events, _last_fetch_time
    now = datetime.now(timezone.utc)
    with _fetch_lock:
        if (
            _cached_events is not None
            and _last_fetch_time is not None
            and (now - _last_fetch_time).total_seconds() < CACHE_TTL_SECONDS
        ):
            return list(_cached_events)

        try:
            response = httpx.get(CALENDAR_URL, timeout=10.0)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or any(not isinstance(event, dict) for event in payload):
                raise ValueError("economic calendar response has an invalid schema")
            _cached_events = payload
            _last_fetch_time = now
            logger.info("updated economic calendar cache (%d events)", len(payload))
            return list(payload)
        except Exception as exc:  # network, HTTP, decoding, or schema failure
            logger.warning("economic calendar unavailable (%s)", type(exc).__name__)
            return None


def _parse_event_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        value = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def check_news_status(settings: Settings | None = None) -> NewsStatus:
    """Pause recommendations during major events or when enabled feed is stale."""
    settings = settings or get_settings()
    if not getattr(settings, "enable_news_filter", True):
        return NewsStatus(is_paused=False, data_available=True)

    events = _fetch_calendar_events()
    if events is None:
        return NewsStatus(
            is_paused=True,
            details="تعذر التحقق من تقويم الأخبار الاقتصادي؛ تم إيقاف التوصيات مؤقتاً لحين استعادة مصدر موثوق.",
            data_available=False,
        )

    try:
        buffer_mins = int(getattr(settings, "news_buffer_minutes", 30))
    except (TypeError, ValueError):
        return NewsStatus(True, "إعداد نافذة الأخبار غير صالح؛ تم إيقاف التوصيات احترازياً.", False)
    if not 0 <= buffer_mins <= 24 * 60:
        return NewsStatus(True, "إعداد نافذة الأخبار خارج النطاق؛ تم إيقاف التوصيات احترازياً.", False)

    now = datetime.now(timezone.utc)
    window = timedelta(minutes=buffer_mins)
    active_events: list[str] = []
    for event in events:
        impact = str(event.get("impact", "")).strip().lower()
        country = str(event.get("country", "")).strip().upper()
        title = str(event.get("title", "خبر اقتصادي هام")).strip()[:160]
        if impact != "high" or country not in MAJOR_CURRENCIES:
            continue
        event_time = _parse_event_date(str(event.get("date", "")))
        if event_time is None:
            continue
        if event_time - window <= now <= event_time + window:
            remaining = int(round((event_time - now).total_seconds() / 60))
            time_desc = f"يصدر بعد {remaining} دقيقة" if remaining > 0 else (
                f"صدر منذ {abs(remaining)} دقيقة" if remaining < 0 else "يصدر الآن"
            )
            active_events.append(f"[{country}] {title} ({time_desc})")

    if active_events:
        return NewsStatus(True, " | ".join(active_events[:3]), True)
    return NewsStatus(False, "", True)
