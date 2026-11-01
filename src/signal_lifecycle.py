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


@dataclass(frozen=True)
class SignalOutcome:
    signal_id: str
    symbol: str
    timeframe: str
    direction: str
    entry_price: float
    exit_price: float
    outcome: str
    r_multiple: float
    tp1_reached: bool
    closed_at: str


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
                    result_sent INTEGER NOT NULL DEFAULT 0
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
        exit_price = float(row["exit_price"])
        risk = abs(entry - float(row["stop_loss"]))
        sign = 1.0 if direction == "BUY" else -1.0
        r_multiple = sign * (exit_price - entry) / risk if risk else 0.0
        return SignalOutcome(
            signal_id=str(row["signal_id"]),
            symbol=str(row["symbol"]),
            timeframe=str(row["timeframe"]),
            direction=direction,
            entry_price=entry,
            exit_price=exit_price,
            outcome=str(row["outcome"]),
            r_multiple=round(r_multiple, 2),
            tp1_reached=bool(row["tp1_reached"]),
            closed_at=str(row["closed_at"]),
        )

    def _close(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        reason: str,
        exit_price: float,
    ) -> SignalOutcome:
        closed_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """UPDATE signals SET status='CLOSED', outcome=?, exit_price=?, closed_at=?
               WHERE signal_id=? AND status='ACTIVE'""",
            (reason, float(exit_price), closed_at, row["signal_id"]),
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
                closed_outcome = self._close(connection, active, "REVERSED", float(signal.entry_price))

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
                        entry_bar_id, last_evaluated_bar, source, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')""",
                    (
                        signal_id, key, symbol, timeframe, direction, strategy,
                        float(signal.entry_price), float(signal.stop_loss_price),
                        float(signal.take_profit_1_price), float(signal.take_profit_2_price),
                        entry_bar_id, entry_bar_id, source,
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
                # OHLC bars do not reveal which level was touched first; count
                # the stop first so the bot never reports an unproven win.
                return self._close(connection, row, "AMBIGUOUS_STOP_FIRST", stop)
            if stop_hit:
                return self._close(connection, row, "STOP_LOSS", stop)
            if tp2_hit:
                return self._close(connection, row, "TAKE_PROFIT_2", tp2)
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
