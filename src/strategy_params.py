"""Loads/saves tunable strategy parameters from `data/strategy_params.json`.

This is the primary mechanism `strategy_evolver.py` uses to test and (after
manual approval) promote parameter changes — editing this JSON is a small,
auditable diff instead of touching strategy code. See `src/ast_patcher.py`
for the secondary, rarely-needed mechanism: surgically patching a literal
constant inside a strategy `.py` file for the (uncommon) case where a value
hasn't been exposed as a JSON parameter yet.

Named `data/strategy_params.json` rather than the more obvious
`config/strategy_params.json` because this project already has a top-level
`config.py` module — a `config/` package and a `config.py` module can't
coexist in the same directory, so this reuses the `data/` directory that
already holds `watchlist.json` for the same kind of durable, mutable state.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from strategies.breakout import BreakoutParams
from strategies.mean_reversion import MeanReversionParams
from strategies.trend_pullback import TrendPullbackParams

logger = logging.getLogger(__name__)

DEFAULT_PARAMS_PATH = Path(__file__).resolve().parent.parent / "data" / "strategy_params.json"

PARAM_CLASSES: dict[str, type] = {
    "trend_pullback": TrendPullbackParams,
    "mean_reversion": MeanReversionParams,
    "breakout": BreakoutParams,
}

_lock = threading.Lock()


def _defaults_payload() -> dict[str, dict[str, Any]]:
    return {name: asdict(cls()) for name, cls in PARAM_CLASSES.items()}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Persist JSON atomically so interruption cannot leave a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise


def _read_payload(path: Path) -> dict[str, Any]:
    """Read a valid JSON object; repair malformed strategy sections in memory."""
    defaults = _defaults_payload()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("failed to load strategy parameters from %s (%s); using code defaults", path, type(exc).__name__)
        return defaults
    if not isinstance(raw, dict):
        logger.error("invalid strategy parameter root in %s; using code defaults", path)
        return defaults

    payload = dict(raw)
    for name, cls in PARAM_CLASSES.items():
        if not isinstance(payload.get(name), dict):
            logger.warning("invalid parameter section %s in %s; using defaults", name, path)
            payload[name] = asdict(cls())
    # Keep unknown dict-valued sections so a future extension is not silently
    # deleted when a current version saves one strategy.
    return payload


def _ensure_file(path: Path) -> None:
    if path.exists():
        return
    _atomic_write_json(path, _defaults_payload())


def _value_matches_default_type(value: Any, default: Any) -> bool:
    if isinstance(default, bool):
        return type(value) is bool
    if isinstance(default, int):
        return type(value) is int
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    return isinstance(value, type(default))


def load_all_params(path: Path = DEFAULT_PARAMS_PATH) -> dict[str, dict[str, Any]]:
    with _lock:
        _ensure_file(path)
        payload = _read_payload(path)
        return {key: value for key, value in payload.items() if isinstance(value, dict)}


def get_strategy_params(
    strategy_name: str, overrides: dict[str, Any] | None = None, path: Path = DEFAULT_PARAMS_PATH
):
    """Returns validated strategy params, layered defaults -> file -> overrides."""
    cls = PARAM_CLASSES.get(strategy_name)
    if cls is None:
        raise ValueError(f"unknown strategy {strategy_name!r}; known: {sorted(PARAM_CLASSES)}")

    defaults = asdict(cls())
    all_params = load_all_params(path)
    stored = all_params.get(strategy_name, {})
    if not isinstance(stored, dict):
        stored = {}
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("parameter overrides must be a dictionary")

    merged = {**defaults, **stored, **(overrides or {})}
    filtered: dict[str, Any] = {}
    for field_info in dataclasses.fields(cls):
        name = field_info.name
        value = merged.get(name, defaults[name])
        if not _value_matches_default_type(value, defaults[name]):
            logger.warning("invalid type/value for strategy parameter %s.%s; using default", strategy_name, name)
            value = defaults[name]
        elif isinstance(defaults[name], float):
            value = float(value)
        filtered[name] = value
    return cls(**filtered)


def save_strategy_params(strategy_name: str, params: Any, path: Path = DEFAULT_PARAMS_PATH) -> None:
    cls = PARAM_CLASSES.get(strategy_name)
    if cls is None:
        raise ValueError(f"unknown strategy {strategy_name!r}; known: {sorted(PARAM_CLASSES)}")
    if not isinstance(params, cls):
        raise TypeError(f"params must be an instance of {cls.__name__}")
    with _lock:
        _ensure_file(path)
        all_params = _read_payload(path)
        all_params[strategy_name] = asdict(params)
        _atomic_write_json(path, all_params)
