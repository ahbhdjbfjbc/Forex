"""Evidence-based review of closed signals; never changes production parameters."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
from urllib.parse import urlparse

import httpx

from config import Settings
from src.signal_lifecycle import signal_lifecycle
from src.storage import get_data_dir

logger = logging.getLogger(__name__)
_PROMPT_VERSION = "closed-outcome-review-v1"
_REPORT_LOCK = threading.Lock()


def _stats(rows: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("r_multiple") is not None:
            grouped.setdefault(str(row.get("strategy", "unknown")), []).append(row)
    summaries = []
    for strategy, items in sorted(grouped.items()):
        rs = [float(item.get("r_multiple", 0.0)) for item in items]
        wins = [value for value in rs if value > 0]
        losses = [value for value in rs if value <= 0]
        gross_loss = abs(sum(losses))
        summaries.append({
            "strategy": strategy,
            "trades": len(items),
            "wins": len(wins),
            "non_wins": len(losses),
            "win_rate_pct": round(100 * len(wins) / len(items), 2),
            "average_r": round(sum(rs) / len(rs), 3),
            "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss else None,
            "mean_quality_score": round(
                sum(float(item["quality_score"]) for item in items if item.get("quality_score") is not None)
                / max(1, sum(item.get("quality_score") is not None for item in items)), 1
            ),
        })
    return {
        "closed_signals": len(rows),
        "resolved_signals": sum(row.get("r_multiple") is not None for row in rows),
        "ambiguous_signals": sum(row.get("r_multiple") is None for row in rows),
        "strategies": summaries,
        "latest_outcomes": [
            {
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "strategy": row.get("strategy"),
                "outcome": row.get("outcome"),
                "direction": row.get("direction"),
                "quality_score": row.get("quality_score"),
                "quality_grade": row.get("quality_grade"),
                "r_multiple": (
                    round(float(row["r_multiple"]), 3) if row.get("r_multiple") is not None else None
                ),
            }
            for row in rows[:20]
        ],
    }


def _local_report(stats: dict) -> str:
    count = stats["resolved_signals"]
    lines = [
        "📊 مراجعة أداء التوصيات المغلقة",
        f"عدد التوصيات المغلقة: {stats['closed_signals']} | نتائج قابلة للحساب: {count} | ملتبسة: {stats['ambiguous_signals']}",
        "المصدر: نتائج التوصيات المحفوظة محلياً؛ لا تعديل آلي للاستراتيجية.",
    ]
    if count == 0:
        lines.append("لا توجد نتائج محسومة كافية حتى الآن؛ لم تُستنتج أنماط أو نسب أداء.")
    else:
        for item in stats["strategies"]:
            pf = "غير محسوب" if item["profit_factor"] is None else str(item["profit_factor"])
            lines.append(
                f"{item['strategy']}: {item['trades']} صفقة، {item['wins']} ربح / "
                f"{item['non_wins']} غير رابحة، متوسط {item['average_r']:+g}R، "
                f"عامل ربح {pf}، متوسط تقييم الشروط {item['mean_quality_score']}"
            )
        if count < 30:
            lines.append("العينة صغيرة (<30): لا تُعامل الإحصاءات كدليل تنبؤي ولا تستنتج أفضلية مستقرة.")
        else:
            lines.append("تظل النتائج تاريخية ومحسوبة من OHLCV؛ افحص الانحياز والعينة قبل أي استنتاج.")
    lines.append("أي تغيير إنتاجي يتطلب اختباراً خارج العينة ومراجعة وموافقة أدمن منفصلة.")
    return "\n".join(lines)


def _remote_review(stats: dict, settings: Settings) -> str | None:
    base_url = str(settings.ai_api_base_url or "").strip()
    api_key = str(settings.ai_api_key or "").strip()
    model = str(settings.ai_model or "").strip()
    parsed = urlparse(base_url)
    if not (parsed.scheme == "https" and parsed.netloc and api_key and model):
        return None
    # Share only aggregate closed-outcome statistics, never keys, chat IDs, or raw messages.
    prompt = (
        "Review these closed trading recommendation outcome statistics. State sample-size limits, "
        "separate observed facts from hypotheses, identify possible loss patterns only when supported, "
        "and suggest validation experiments. Do not claim a win probability, give execution instructions, "
        "or recommend changing production parameters. Reply in Arabic and concise. Data: "
        + json.dumps(stats, ensure_ascii=False, separators=(",", ":"))
    )
    try:
        response = httpx.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 700,
            },
            timeout=20.0,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("AI provider returned an empty response")
        return content.strip()[:3000]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("optional AI review unavailable (%s)", type(exc).__name__)
        return None


def generate_review(settings: Settings) -> str:
    """Build a local evidence-based report and optionally request a remote review."""
    rows = signal_lifecycle.closed_history(limit=500)
    stats = _stats(rows)
    report = _local_report(stats)
    model_name = "local-statistics-v1"
    if settings.ai_review_enabled:
        remote = _remote_review(stats, settings)
        if remote:
            report += "\n\n🧠 مراجعة النموذج (اقتراحات لا تغيّر الإنتاج):\n" + remote
            model_name = settings.ai_model
        else:
            report += "\n\nلم تتوفر بيانات مزود AI صالحة؛ أُعد هذا التقرير محلياً دون نموذج خارجي."

    generated_at = datetime.now(timezone.utc).isoformat()
    record = {
        "generated_at": generated_at,
        "prompt_version": _PROMPT_VERSION,
        "model": model_name,
        "statistics": stats,
        "report": report,
    }
    report_dir = get_data_dir() / "ai_reviews"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return report


def maybe_send_periodic_report(settings: Settings) -> bool:
    """Send a private, rate-limited report to the configured numeric admin only."""
    if not settings.ai_review_enabled:
        return False
    admin_id = str(settings.telegram_allowed_user_id or "").strip()
    if not admin_id.isdecimal() or not settings.telegram_bot_token:
        logger.warning("periodic AI report skipped: Telegram admin ID or bot token is not configured")
        return False

    marker = get_data_dir() / "ai_reviews" / "last_sent.json"
    with _REPORT_LOCK:
        now = datetime.now(timezone.utc)
        if marker.exists():
            try:
                last_sent = datetime.fromisoformat(json.loads(marker.read_text(encoding="utf-8"))["sent_at"])
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                if (now - last_sent).total_seconds() < settings.ai_report_interval_hours * 3600:
                    return False
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                logger.warning("invalid AI report timer state; regenerating a report")

        report = generate_review(settings)
        from src.notifier import send_admin_message

        if not send_admin_message(report, settings):
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"sent_at": now.isoformat()}), encoding="utf-8")
        temporary.replace(marker)
        return True
