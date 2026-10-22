"""Dynamic, file-backed watchlist for the 3-hour auto-scan scheduler.

Holds the set of canonical symbols (see `src.symbol_resolver`) that
`main.py`'s scheduled job scans every `AUTO_SCAN_INTERVAL_SECONDS`.
Telegram's `/add`, `/remove`, `/watchlist` commands (`src/telegram_bot.py`)
mutate this store; a successful on-demand `/scan` also auto-adds its symbol.

Backed by a small JSON file so it survives process restarts. This is
deliberately not a database — just enough durability that restarting the
bot doesn't silently drop what you're tracking. Access is guarded by a lock
since the scheduled scan runs on APScheduler's own thread while Telegram
commands run on the asyncio event loop thread.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from src.symbol_resolver import sanitize_symbol

logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST = ["EURUSD", "GBPUSD", "XAUUSD", "USDJPY"]
DEFAULT_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"


class WatchlistStore:
    def __init__(self, path: Path = DEFAULT_STORE_PATH, defaults: list[str] | None = None):
        self._path = path
        raw_defaults = defaults if defaults is not None else DEFAULT_WATCHLIST
        self._defaults = sorted({
            symbol for raw in raw_defaults
            if isinstance(raw, str) and (symbol := sanitize_symbol(raw)) is not None
        })
        self._lock = threading.Lock()
        self._symbols: set[str] = set()
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self._path.exists():
                try:
                    data = json.loads(self._path.read_text(encoding="utf-8"))
                    raw_symbols = data.get("symbols", []) if isinstance(data, dict) else None
                    if not isinstance(raw_symbols, list):
                        raise ValueError("watchlist must be a JSON object with a symbols list")
                    loaded = {
                        symbol for raw in raw_symbols
                        if isinstance(raw, str) and (symbol := sanitize_symbol(raw)) is not None
                    }
                    if loaded:
                        self._symbols = loaded
                        # Normalize legacy or hand-edited files on disk.
                        self._save_locked()
                        return
                except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
                    logger.warning(
                        "failed to load watchlist file %s (%s); falling back to defaults",
                        self._path, type(exc).__name__,
                    )
            self._symbols = set(self._defaults)
            self._save_locked()

    def _save_locked(self) -> None:
        """Persist the watchlist atomically so interruption cannot leave half JSON."""
        temp_path: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._path.parent,
                prefix=f".{self._path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = handle.name
                handle.write(json.dumps({"symbols": sorted(self._symbols)}, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
        except OSError as exc:
            logger.error("failed to persist watchlist to %s: %s", self._path, exc)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def get_all(self) -> list[str]:
        with self._lock:
            return sorted(self._symbols)

    def add(self, raw_symbol: str) -> tuple[str | None, bool]:
        """Returns (canonical_symbol, was_newly_added). canonical_symbol is
        None if `raw_symbol` didn't sanitize to a valid ticker."""
        symbol = sanitize_symbol(raw_symbol)
        if symbol is None:
            return None, False
        with self._lock:
            was_new = symbol not in self._symbols
            self._symbols.add(symbol)
            if was_new:
                self._save_locked()
        return symbol, was_new

    def remove(self, raw_symbol: str) -> tuple[str | None, bool]:
        """Returns (canonical_symbol, was_removed). canonical_symbol is None
        if `raw_symbol` didn't sanitize to a valid ticker."""
        symbol = sanitize_symbol(raw_symbol)
        if symbol is None:
            return None, False
        with self._lock:
            existed = symbol in self._symbols
            self._symbols.discard(symbol)
            if existed:
                self._save_locked()
        return symbol, existed

    def contains(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._symbols


watchlist_store = WatchlistStore()
