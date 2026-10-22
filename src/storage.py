"""Resolve the directory used for mutable bot state."""
from __future__ import annotations

from pathlib import Path

from config import get_settings


def get_data_dir() -> Path:
    """Use the configured persistent directory or the repository's data folder."""
    configured = get_settings().forex_data_dir
    if configured is not None:
        return configured.expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "data"
