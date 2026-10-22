"""Shared schemas used across scanner, bridge, and notifier modules.

Anything that crosses a network/process boundary (webhook payloads) is a
pydantic BaseModel. Internal-only value objects that never get (de)serialized
are dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
import re

from pydantic import BaseModel, Field, field_validator


class MarketRegime(str, Enum):
    STRONG_TREND_BULLISH = "STRONG_TREND_BULLISH"
    STRONG_TREND_BEARISH = "STRONG_TREND_BEARISH"
    RANGING_CONSOLIDATION = "RANGING_CONSOLIDATION"
    HIGH_VOLATILITY_EXPANSION = "HIGH_VOLATILITY_EXPANSION"
    LOW_LIQUIDITY_CHOP = "LOW_LIQUIDITY_CHOP"


class ForexSession(str, Enum):
    ASIAN = "ASIAN"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    LONDON_NY_OVERLAP = "LONDON_NY_OVERLAP"
    OFF_HOURS = "OFF_HOURS"


class StrategyType(str, Enum):
    TREND_PULLBACK = "TREND_PULLBACK"
    MEAN_REVERSION_FADE = "MEAN_REVERSION_FADE"
    VOLATILITY_BREAKOUT = "VOLATILITY_BREAKOUT"
    NO_TRADE = "NO_TRADE"


class TradeDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class StructuralMetrics:
    """Raw indicator readings for one (symbol, timeframe) snapshot."""

    adx: float
    ema_20: float
    ema_50: float
    ema_200: float
    higher_highs: bool
    lower_lows: bool
    atr: float
    atr_avg: float
    bb_bandwidth: float
    bb_bandwidth_percentile: float
    rvol: float
    session: ForexSession
    close: float


@dataclass(frozen=True)
class RegimeResult:
    """Output of Scanner 1 for one (symbol, timeframe) pair."""

    symbol: str
    timeframe: str
    regime: MarketRegime
    metrics: StructuralMetrics
    confidence: float  # 0..1, how strongly the rules matched
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class RiskParameters:
    account_size_usd: float
    risk_percent: float
    stop_loss_pips: float
    pip_value_per_lot: float
    lot_size: float
    risk_amount_usd: float


@dataclass(frozen=True)
class TradeSignal:
    """Output of Scanner 2 — a concrete, actionable trade idea."""

    symbol: str
    timeframe: str
    direction: TradeDirection
    strategy: StrategyType
    regime: MarketRegime
    entry_price: float
    stop_loss_price: float
    stop_loss_pips: float
    take_profit_1_price: float
    take_profit_1_pips: float
    take_profit_1_rr: float
    take_profit_2_price: float
    take_profit_2_pips: float
    take_profit_2_rr: float
    risk: RiskParameters
    rationale: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------
# Network-facing schemas (TradingView webhook payloads)
# --------------------------------------------------------------------------


class TradingViewAlert(BaseModel):
    """Expected JSON body of a TradingView Pine Script alert webhook.

    Example payload (configured via the Pine `alert()` message template):
        {
            "ticker": "EURUSD",
            "timeframe": "15m",
            "signal": "BUY",
            "price": 1.0850,
            "secret": "change-me-to-a-long-random-string"
        }
    """

    ticker: str = Field(..., min_length=3, max_length=32)
    timeframe: str = Field(..., min_length=1, max_length=16)
    signal: TradeDirection | str
    price: float = Field(..., gt=0)
    # Credentials are excluded from model dumps and repr to avoid accidental
    # disclosure in diagnostics or future serialization paths.
    secret: str | None = Field(default=None, repr=False, exclude=True, max_length=512)
    strategy: str | None = Field(default=None, max_length=64)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)

    @field_validator("price", "stop_loss", "take_profit")
    @classmethod
    def _finite_positive_price(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("price values must be finite and positive")
        return value

    @field_validator("timeframe")
    @classmethod
    def _normalize_timeframe(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{1,16}", normalized):
            raise ValueError("timeframe contains invalid characters")
        return normalized

    @field_validator("strategy")
    @classmethod
    def _normalize_strategy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or not re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", normalized):
            raise ValueError("strategy contains invalid characters")
        return normalized

    @field_validator("signal")
    @classmethod
    def _normalize_signal(cls, v: str) -> str:
        v_upper = str(v).upper().strip()
        if v_upper not in {"BUY", "SELL"}:
            raise ValueError(f"signal must be BUY or SELL, got {v!r}")
        return v_upper

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, v: str) -> str:
        normalized = v.strip().upper()
        # TradingView symbols are commonly sent as EXCHANGE:SYMBOL; retain
        # only the symbol component so downstream sizing sees EURUSD/BTCUSD.
        if ":" in normalized:
            normalized = normalized.rsplit(":", 1)[-1]
        normalized = normalized.replace("/", "").strip()
        if not normalized or not re.fullmatch(r"[A-Z0-9=._!-]{3,32}", normalized):
            raise ValueError("ticker contains invalid characters")
        return normalized


class WebhookAck(BaseModel):
    status: str
    detail: str | None = None
