"""Economic News Filter — detects high-impact forex news events.

Fetches the weekly economic calendar feed, caches it in memory to avoid
rate-limiting, and checks whether the current UTC time falls within
`settings.news_buffer_minutes` before or after any High-Impact event.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import httpx

from config import Settings, get_settings

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_TTL_SECONDS = 3600 * 3  # تحديث جدول الأخبار كل 3 ساعات تلقائياً

MAJOR_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}

_cached_events: list[dict[str, Any]] = []
_last_fetch_time: datetime | None = None


@dataclass
class NewsStatus:
    is_paused: bool
    details: str = ""


def _fetch_calendar_events() -> list[dict[str, Any]]:
    global _cached_events, _last_fetch_time
    now = datetime.now(timezone.utc)

    if (
        _last_fetch_time is not None
        and (now - _last_fetch_time).total_seconds() < CACHE_TTL_SECONDS
        and _cached_events
    ):
        return _cached_events

    try:
        resp = httpx.get(CALENDAR_URL, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                _cached_events = data
                _last_fetch_time = now
                logger.info("updated economic calendar cache (%d events)", len(data))
    except Exception as exc:
        logger.debug("could not refresh economic calendar (%s), using cached state", type(exc).__name__)

    return _cached_events


def _parse_event_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def check_news_status(settings: Settings | None = None) -> NewsStatus:
    """يفحص ما إذا كان هناك خبر اقتصادي قوي (High Impact) يؤثر على السوق حالياً."""
    settings = settings or get_settings()
    if not getattr(settings, "enable_news_filter", True):
        return NewsStatus(is_paused=False)

    events = _fetch_calendar_events()
    if not events:
        return NewsStatus(is_paused=False)

    now = datetime.now(timezone.utc)
    buffer_mins = getattr(settings, "news_buffer_minutes", 30)
    window = timedelta(minutes=buffer_mins)

    active_events: list[str] = []

    for ev in events:
        impact = str(ev.get("impact", "")).strip().lower()
        country = str(ev.get("country", "")).strip().upper()
        title = str(ev.get("title", "خبر اقتصادي هام")).strip()

        if impact != "high" or country not in MAJOR_CURRENCIES:
            continue

        ev_time = _parse_event_date(str(ev.get("date", "")))
        if ev_time is None:
            continue

        if (ev_time - window) <= now <= (ev_time + window):
            diff_mins = int(round((ev_time - now).total_seconds() / 60))
            if diff_mins > 0:
                time_desc = f"يصدر بعد {diff_mins} دقيقة"
            elif diff_mins < 0:
                time_desc = f"صدر منذ {abs(diff_mins)} دقيقة"
            else:
                time_desc = "يصدر الآن"
            active_events.append(f"[{country}] {title} ({time_desc})")

    if active_events:
        return NewsStatus(
            is_paused=True,
            details=" | ".join(active_events[:3]),
        )

    return NewsStatus(is_paused=False)
