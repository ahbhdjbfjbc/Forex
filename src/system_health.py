"""Persistent, transition-based health alerts for essential market services."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import threading

from config import Settings
from src.storage import get_data_dir

logger = logging.getLogger(__name__)
_HEALTH_LOCK = threading.Lock()
_COMPONENT_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


def _read_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def report_health(
    component: str,
    healthy: bool,
    details: str,
    settings: Settings,
    sender=None,
) -> bool:
    """Persist health and notify the configured admin once per state transition.

    Repeated failures are suppressed. A failed Telegram send is retried on the
    next monitor tick; a recovery notice is sent when the component is healthy.
    """
    if not isinstance(component, str) or not _COMPONENT_RE.fullmatch(component):
        raise ValueError("invalid health component name")
    if type(healthy) is not bool:
        raise ValueError("healthy must be bool")
    path = get_data_dir() / "health_state.json"
    with _HEALTH_LOCK:
        states = _read_state(path)
        previous = states.get(component)
        previous_healthy = previous.get("healthy") if isinstance(previous, dict) else None
        notified_state = previous.get("notified_state") if isinstance(previous, dict) else None
        is_transition = previous_healthy is not None and previous_healthy != healthy
        initial_failure = previous_healthy is None and not healthy
        retry_needed = notified_state is not healthy
        should_notify = initial_failure or is_transition or retry_needed

        if healthy and previous_healthy is None:
            # Normal startup does not generate a noisy "everything is fine" notice.
            should_notify = False
            notified_state = True

        sent = False
        if should_notify:
            if sender is None:
                from src.notifier import send_admin_message

                sender = send_admin_message
            now = datetime.now(timezone.utc).isoformat()
            safe_details = str(details or "").strip().replace("\n", " ")[:300]
            if healthy:
                message = f"✅ تعافي الخدمة الأساسية: {component}\nالحالة: عادت للعمل.\n{safe_details}"
            else:
                message = f"🚨 عطل في خدمة أساسية: {component}\nالحالة: متوقفة احترازياً.\n{safe_details}"
            try:
                sent = bool(sender(message, settings))
            except Exception as exc:
                logger.warning("health alert delivery failed (%s)", type(exc).__name__)
            if sent:
                notified_state = healthy
                logger.warning("health transition reported: component=%s healthy=%s at=%s", component, healthy, now)

        states[component] = {
            "healthy": healthy,
            "details": str(details or "").strip().replace("\n", " ")[:300],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "notified_state": notified_state,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return sent
