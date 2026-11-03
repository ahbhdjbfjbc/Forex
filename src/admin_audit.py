"""Minimal redacted audit log for Telegram administrative interactions."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading

from src.storage import get_data_dir

logger = logging.getLogger(__name__)
_AUDIT_LOCK = threading.Lock()
_SAFE_ACTION = re.compile(r"^[A-Za-z0-9_:/.-]{1,64}$")


def record_admin_action(actor_id: str | int | None, action: str, result: str) -> None:
    """Append a redacted action/result event; do not store command arguments."""
    action = action if isinstance(action, str) and _SAFE_ACTION.fullmatch(action) else "unknown"
    result = result if result in {"authorized", "denied", "succeeded", "failed"} else "unknown"
    actor = str(actor_id) if actor_id is not None and str(actor_id).isdecimal() else "unknown"
    directory = get_data_dir()
    path = directory / "admin_audit.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor_user_id": actor,
        "action": action,
        "result": result,
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            if path.exists() and path.stat().st_size > 2_000_000:
                backup = path.with_suffix(".jsonl.1")
                os.replace(path, backup)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("could not persist admin audit event (%s)", type(exc).__name__)
