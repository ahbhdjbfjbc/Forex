"""Durable de-duplication and outcome tracking for published trade signals.

The store is process-safe for a single persistent data volume. It prevents a
second active signal for the same normalized symbol/timeframe, records TP/SL
resolution once, and retains unsent outcomes as a small notification outbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from src.storage import get_data_dir
from src.risk import pip_size
from src.symbol_resolver import instrument_type


@dataclass(frozen=True)
class SignalOutcome:
    signal_id: str
    symbol: str
    timeframe: str
    direction: str
    entry_price: float
    exit_price: float | None
    outcome: str
    r_multiple: float | None
    tp1_reached: bool
    closed_at: str
    strategy: str
    stop_loss_price: float
    target_price: float
    source: str
    points: float | None
    points_unit: str
    duration_seconds: int
    opened_at: str


@dataclass(frozen=True)
class SignalRegistration:
    accepted: bool
    duplicate: bool
    signal_id: str | None = None
    closed_outcome: SignalOutcome | None = None


def normalize_symbol(symbol: str) -> str:
    value = str(symbol).upper().strip()
    if ":" in value:
        value = value.rsplit(":", 1)[-1]
    return value.replace("=X", "").replace("/", "").replace("_", "")


def normalize_timeframe(timeframe: str) -> str:
    value = str(timeframe).strip().lower()
    aliases = {
        "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
        "45": "45m", "60": "1h", "120": "2h", "180": "3h",
        "240": "4h", "d": "1d", "1d": "1d", "w": "1w", "1w": "1w",
    }
    return aliases.get(value, value)


def market_key(symbol: str, timeframe: str) -> str:
    return f"{normalize_symbol(symbol)}:{normalize_timeframe(timeframe)}"


def normalize_bar_id(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value))
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        return str(int(timestamp() * 1000))
    text = str(value).strip()
    if text.isdigit():
        return str(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return str(int(parsed.timestamp() * 1000))


def _parse_bar_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            parsed = datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _current_strategy_version() -> str | None:
    """Short fingerprint of the live strategy parameter file (for version comparison)."""
    try:
        import hashlib

        from src.strategy_params import DEFAULT_PARAMS_PATH as PARAMS_PATH

        return hashlib.sha256(Path(PARAMS_PATH).read_bytes()).hexdigest()[:12]
    except Exception:
        return None


class SignalLifecycleStore:
    """SQLite-backed active-signal registry and one-shot outcome outbox."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else get_data_dir() / "signal_lifecycle.sqlite3"
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,
                    market_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit_1 REAL NOT NULL,
                    take_profit_2 REAL NOT NULL,
                    entry_bar_id TEXT,
                    last_evaluated_bar TEXT,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'CLOSED', 'CANCELLED')),
                    tp1_reached INTEGER NOT NULL DEFAULT 0,
                    setup_reset INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT,
                    exit_price REAL,
                    closed_at TEXT,
                    result_sent INTEGER NOT NULL DEFAULT 0,
                    chart_path TEXT,
                    source_data_path TEXT,
                    opened_at TEXT,
                    close_bar_id TEXT,
                    quality_score INTEGER,
                    quality_grade TEXT,
                    telegram_message_id INTEGER,
                    strategy_version TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_signal_per_market
                    ON signals(market_key) WHERE status = 'ACTIVE';
                CREATE INDEX IF NOT EXISTS pending_signal_outcomes
                    ON signals(status, result_sent, closed_at);
                CREATE TABLE IF NOT EXISTS blocked_setups (
                    market_key TEXT PRIMARY KEY,
                    direction TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(signals)").fetchall()
            }
            if "setup_reset" not in columns:
                connection.execute(
                    "ALTER TABLE signals ADD COLUMN setup_reset INTEGER NOT NULL DEFAULT 0"
                )
            if "last_evaluated_bar" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN last_evaluated_bar TEXT")
            if "chart_path" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN chart_path TEXT")
            if "source_data_path" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN source_data_path TEXT")
            if "opened_at" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN opened_at TEXT")
            if "close_bar_id" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN close_bar_id TEXT")
            if "quality_score" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN quality_score INTEGER")
            if "quality_grade" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN quality_grade TEXT")
            if "telegram_message_id" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN telegram_message_id INTEGER")
            if "strategy_version" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN strategy_version TEXT")

    def set_chart_assets(self, signal_id: str, image_path: str, source_data_path: str) -> bool:
        """Persist local chart/source references before any recommendation is sent."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE signals SET chart_path=?, source_data_path=?
                   WHERE signal_id=? AND status='ACTIVE'""",
                (str(image_path), str(source_data_path), signal_id),
            )
            return cursor.rowcount == 1

    def set_telegram_message_id(self, signal_id: str, message_id: int) -> None:
        """Remember the channel message so the result can be posted as a reply to it."""
        if not signal_id or not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE signals SET telegram_message_id=? WHERE signal_id=?",
                (int(message_id), signal_id),
            )

    def telegram_message_id(self, signal_id: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT telegram_message_id FROM signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
        if row is None or row["telegram_message_id"] is None:
            return None
        return int(row["telegram_message_id"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _value(value) -> str:
        return str(getattr(value, "value", value))

    @staticmethod
    def _outcome_from_row(row: sqlite3.Row) -> SignalOutcome:
        direction = str(row["direction"])
        entry = float(row["entry_price"])
        exit_price = float(row["exit_price"]) if row["exit_price"] is not None else None
        symbol = str(row["symbol"])
        sign = 1.0 if direction == "BUY" else -1.0
        if exit_price is not None:
            risk = abs(entry - float(row["stop_loss"]))
            r_multiple = sign * (exit_price - entry) / risk if risk else 0.0
            signed_move = sign * (exit_price - entry)
        else:
            r_multiple = None
            signed_move = None
        if instrument_type(symbol) == "FX":
            points, points_unit = (signed_move / pip_size(symbol) if signed_move is not None else None), "pips"
        else:
            points, points_unit = signed_move, "price units"
        entry_time = _parse_bar_datetime(row["entry_bar_id"]) or _parse_bar_datetime(row["opened_at"])
        exit_time = _parse_bar_datetime(row["close_bar_id"]) or _parse_bar_datetime(row["closed_at"])
        duration = max(0, int((exit_time - entry_time).total_seconds())) if entry_time and exit_time else 0
        return SignalOutcome(
            signal_id=str(row["signal_id"]),
            symbol=str(row["symbol"]),
            timeframe=str(row["timeframe"]),
            direction=direction,
            entry_price=entry,
            exit_price=exit_price,
            outcome=str(row["outcome"]),
            r_multiple=round(r_multiple, 2) if r_multiple is not None else None,
            tp1_reached=bool(row["tp1_reached"]),
            closed_at=str(row["closed_at"]),
            strategy=str(row["strategy"]),
            stop_loss_price=float(row["stop_loss"]),
            target_price=float(row["take_profit_2"]),
            source=str(row["source"]),
            points=round(points, 1 if points_unit == "pips" else 5) if points is not None else None,
            points_unit=points_unit,
            duration_seconds=duration,
            opened_at=str(row["opened_at"] or ""),
        )

    def _close(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        reason: str,
        exit_price: float | None,
        bar_id: str | None = None,
    ) -> SignalOutcome:
        bar_id = normalize_bar_id(bar_id)
        close_time = _parse_bar_datetime(bar_id)
        closed_at = (close_time or datetime.now(timezone.utc)).isoformat()
        connection.execute(
            """UPDATE signals SET status='CLOSED', outcome=?, exit_price=?, closed_at=?, close_bar_id=?
               WHERE signal_id=? AND status='ACTIVE'""",
            (reason, float(exit_price) if exit_price is not None else None, closed_at, bar_id, row["signal_id"]),
        )
        if not row["setup_reset"]:
            connection.execute(
                """INSERT INTO blocked_setups(market_key, direction) VALUES (?, ?)
                   ON CONFLICT(market_key) DO UPDATE SET direction=excluded.direction""",
                (row["market_key"], row["direction"]),
            )
        closed = connection.execute(
            "SELECT * FROM signals WHERE signal_id=?", (row["signal_id"],)
        ).fetchone()
        return self._outcome_from_row(closed)

    def register_signal(
        self, signal, *, source: str = "unknown", entry_bar_id: str | None = None
    ) -> SignalRegistration:
        symbol = str(signal.symbol).upper().strip()
        timeframe = normalize_timeframe(signal.timeframe)
        key = market_key(symbol, timeframe)
        direction = self._value(signal.direction).upper()
        strategy = self._value(signal.strategy)
        entry_bar_id = normalize_bar_id(entry_bar_id)
        signal_id = uuid4().hex
        closed_outcome = None

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT * FROM signals WHERE market_key=? AND status='ACTIVE'", (key,)
            ).fetchone()
            if active is not None:
                if active["direction"] == direction:
                    connection.rollback()
                    return SignalRegistration(accepted=False, duplicate=True)
                closed_outcome = self._close(
                    connection, active, "REVERSED", float(signal.entry_price), bar_id=entry_bar_id
                )

            blocked = connection.execute(
                "SELECT direction FROM blocked_setups WHERE market_key=?", (key,)
            ).fetchone()
            if blocked is not None and blocked["direction"] == direction:
                connection.commit()
                return SignalRegistration(
                    accepted=False, duplicate=True, closed_outcome=closed_outcome
                )

            # A new direction is a fresh setup and releases the previous
            # direction's post-close block for future, genuinely new setups.
            connection.execute("DELETE FROM blocked_setups WHERE market_key=?", (key,))
            try:
                connection.execute(
                    """INSERT INTO signals(
                        signal_id, market_key, symbol, timeframe, direction, strategy,
                        entry_price, stop_loss, take_profit_1, take_profit_2,
                        entry_bar_id, last_evaluated_bar, source, status, opened_at,
                        quality_score, quality_grade, strategy_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?)""",
                    (
                        signal_id, key, symbol, timeframe, direction, strategy,
                        float(signal.entry_price), float(signal.stop_loss_price),
                        float(signal.take_profit_1_price), float(signal.take_profit_2_price),
                        entry_bar_id, entry_bar_id, source,
                        getattr(signal, "timestamp", datetime.now(timezone.utc)).isoformat(),
                        getattr(signal, "quality_score", None), getattr(signal, "quality_grade", None),
                        _current_strategy_version(),
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return SignalRegistration(accepted=False, duplicate=True)
            connection.commit()
        return SignalRegistration(
            accepted=True, duplicate=False, signal_id=signal_id,
            closed_outcome=closed_outcome,
        )

    def cancel_signal(self, signal_id: str | None) -> None:
        if not signal_id:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE signals SET status='CANCELLED' WHERE signal_id=? AND status='ACTIVE'",
                (signal_id,),
            )

    def _evaluate(
        self, symbol: str, timeframe: str, high: float, low: float, close: float,
        bar_id: str | None = None, mark_evaluated: bool = False,
    ) -> SignalOutcome | None:
        key = market_key(symbol, timeframe)
        bar_id = normalize_bar_id(bar_id)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM signals WHERE market_key=? AND status='ACTIVE'", (key,)
            ).fetchone()
            if row is None or (bar_id is not None and row["entry_bar_id"] == str(bar_id)):
                connection.commit()
                return None

            direction = str(row["direction"])
            stop = float(row["stop_loss"])
            tp1 = float(row["take_profit_1"])
            tp2 = float(row["take_profit_2"])
            if direction == "BUY":
                stop_hit = low <= stop
                tp1_hit = high >= tp1
                tp2_hit = high >= tp2
            else:
                stop_hit = high >= stop
                tp1_hit = low <= tp1
                tp2_hit = low <= tp2

            if stop_hit and (tp1_hit or tp2_hit):
                # OHLC bars do not reveal level order. Close as ambiguous and
                # store no exit/P&L rather than inventing a win or a loss.
                return self._close(connection, row, "AMBIGUOUS", None, bar_id)
            if stop_hit:
                return self._close(connection, row, "STOP_LOSS", stop, bar_id)
            if tp2_hit:
                return self._close(connection, row, "TAKE_PROFIT_2", tp2, bar_id)
            if tp1_hit and not row["tp1_reached"]:
                connection.execute(
                    "UPDATE signals SET tp1_reached=1 WHERE signal_id=? AND status='ACTIVE'",
                    (row["signal_id"],),
                )
            if mark_evaluated and bar_id is not None:
                connection.execute(
                    "UPDATE signals SET last_evaluated_bar=? WHERE signal_id=? AND status='ACTIVE'",
                    (bar_id, row["signal_id"]),
                )
            connection.commit()
            return None

    def observe_price(
        self, symbol: str, timeframe: str, price: float
    ) -> SignalOutcome | None:
        value = float(price)
        return self._evaluate(symbol, timeframe, value, value, value)

    def observe_bar(
        self, symbol: str, timeframe: str, *, high: float, low: float, close: float,
        bar_id: str | None = None, mark_evaluated: bool = False,
    ) -> SignalOutcome | None:
        return self._evaluate(
            symbol, timeframe, float(high), float(low), float(close), bar_id, mark_evaluated
        )

    def observe_frame(self, symbol: str, timeframe: str, frame) -> SignalOutcome | None:
        """Evaluate every unseen bar after entry; keep rechecking the newest bar."""
        key = market_key(symbol, timeframe)
        with self._connect() as connection:
            active = connection.execute(
                "SELECT entry_bar_id, last_evaluated_bar FROM signals "
                "WHERE market_key=? AND status='ACTIVE'", (key,)
            ).fetchone()
        if active is None or active["entry_bar_id"] is None or frame is None or frame.empty:
            return None

        bar_ids = [normalize_bar_id(value) for value in frame.index]
        cursor = active["last_evaluated_bar"] or active["entry_bar_id"]
        try:
            cursor_index = bar_ids.index(cursor)
        except ValueError:
            start_index = max(0, len(bar_ids) - 1)
        else:
            start_index = cursor_index if cursor_index == len(bar_ids) - 1 else cursor_index + 1

        for index in range(start_index, len(bar_ids)):
            outcome = self.observe_bar(
                symbol,
                timeframe,
                high=float(frame["High"].iloc[index]),
                low=float(frame["Low"].iloc[index]),
                close=float(frame["Close"].iloc[index]),
                bar_id=bar_ids[index],
                mark_evaluated=index < len(bar_ids) - 1,
            )
            if outcome is not None:
                return outcome
        return None

    def reset_block(self, symbol: str, timeframe: str) -> None:
        key = market_key(symbol, timeframe)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM blocked_setups WHERE market_key=?", (key,))
            connection.execute(
                "UPDATE signals SET setup_reset=1 WHERE market_key=? AND status='ACTIVE'",
                (key,),
            )

    def active_symbols(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM signals WHERE status='ACTIVE' ORDER BY symbol"
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def closed_history(self, limit: int = 500) -> list[dict]:
        """Return closed recommendation snapshots for local performance analysis."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError("history limit must be between 1 and 10000")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT signal_id, symbol, timeframe, direction, strategy, source,
                          entry_price, stop_loss, take_profit_1, take_profit_2,
                          exit_price, outcome, closed_at, opened_at, tp1_reached,
                          quality_score, quality_grade, chart_path, source_data_path,
                          strategy_version
                   FROM signals WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            risk = abs(float(item["entry_price"]) - float(item["stop_loss"]))
            sign = 1.0 if item["direction"] == "BUY" else -1.0
            item["r_multiple"] = (
                sign * (float(item["exit_price"]) - float(item["entry_price"])) / risk
                if risk and item["exit_price"] is not None else None
            )
            history.append(item)
        return history

    _LIST_COLUMNS = (
        "signal_id, symbol, timeframe, direction, strategy, source, status, entry_price, "
        "stop_loss, take_profit_1, take_profit_2, exit_price, outcome, opened_at, closed_at, "
        "entry_bar_id, close_bar_id, tp1_reached, quality_score, quality_grade, chart_path, "
        "source_data_path, result_sent, strategy_version, telegram_message_id"
    )

    def _enrich(self, row: sqlite3.Row) -> dict:
        item = dict(row)
        item["r_multiple"] = None
        item["points"] = None
        item["points_unit"] = "pips" if instrument_type(item["symbol"]) == "FX" else "price units"
        item["duration_seconds"] = None
        if item.get("status") == "CLOSED":
            outcome = self._outcome_from_row(row)
            item["r_multiple"] = outcome.r_multiple
            item["points"] = outcome.points
            item["duration_seconds"] = outcome.duration_seconds
        return item

    def list_signals(self, *, limit: int = 10, offset: int = 0, include_active: bool = True) -> list[dict]:
        """Published recommendations, newest first (cancelled/unsent drafts are excluded)."""
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        statuses = ("ACTIVE", "CLOSED") if include_active else ("CLOSED",)
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._LIST_COLUMNS} FROM signals WHERE status IN ({placeholders}) "
                "ORDER BY COALESCE(opened_at, closed_at) DESC, signal_id LIMIT ? OFFSET ?",
                (*statuses, limit, offset),
            ).fetchall()
        return [self._enrich(row) for row in rows]

    def count_signals(self, include_active: bool = True) -> int:
        statuses = ("ACTIVE", "CLOSED") if include_active else ("CLOSED",)
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM signals WHERE status IN ({placeholders})", statuses
            ).fetchone()
        return int(row["n"])

    def get_signal(self, signal_id_prefix: str) -> dict | None:
        """Look up one published recommendation by its full ID or a unique 6+ char prefix."""
        prefix = str(signal_id_prefix or "").strip().lower()
        if len(prefix) < 6 or not all(ch in "0123456789abcdef" for ch in prefix):
            return None
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._LIST_COLUMNS} FROM signals WHERE signal_id LIKE ? "
                "AND status IN ('ACTIVE', 'CLOSED') LIMIT 2",
                (prefix + "%",),
            ).fetchall()
        if len(rows) != 1:
            return None
        return self._enrich(rows[0])

    def performance_summary(self) -> dict:
        """Counts and realized R over every closed recommendation (nothing is hidden)."""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM signals WHERE status='CLOSED'").fetchall()
            active = connection.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE status='ACTIVE'"
            ).fetchone()["n"]
        wins = losses = ambiguous = reversed_count = 0
        total_r = 0.0
        for row in rows:
            outcome = self._outcome_from_row(row)
            if outcome.r_multiple is None:
                ambiguous += 1
                continue
            total_r += outcome.r_multiple
            if outcome.outcome == "REVERSED":
                reversed_count += 1
            if outcome.r_multiple > 0:
                wins += 1
            else:
                losses += 1
        resolved = wins + losses
        return {
            "active": int(active),
            "closed": len(rows),
            "wins": wins,
            "losses": losses,
            "ambiguous": ambiguous,
            "reversed": reversed_count,
            "win_rate_pct": round(100.0 * wins / resolved, 1) if resolved else None,
            "total_r": round(total_r, 2),
            "average_r": round(total_r / resolved, 3) if resolved else None,
        }

    def active_markets(self) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT symbol, timeframe FROM signals WHERE status='ACTIVE' ORDER BY symbol"
            ).fetchall()
        return [(str(row["symbol"]), str(row["timeframe"])) for row in rows]

    def pending_outcomes(self) -> list[SignalOutcome]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM signals WHERE status='CLOSED' AND result_sent=0 ORDER BY closed_at"
            ).fetchall()
        return [self._outcome_from_row(row) for row in rows]

    def mark_outcome_sent(self, signal_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE signals SET result_sent=1 WHERE signal_id=? AND status='CLOSED'",
                (signal_id,),
            )


signal_lifecycle = SignalLifecycleStore()
