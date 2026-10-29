"""Single source of truth for runtime configuration.

Everything here is loaded from `.env` (see `.env.example`). No other module
should call `os.environ` directly — import `get_settings()` instead.
"""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Watchlist & timeframes
    watchlist: str = "EURUSD=X,GBPUSD=X,USDJPY=X,AUDUSD=X,USDCAD=X,NZDUSD=X,USDCHF=X"
    timeframes: str = "4h,1h,15m"

    # Risk management
    account_size_usd: float = 10_000.0
    risk_percent_per_trade: float = 1.0
    min_risk_reward: float = 2.0

    # Regime thresholds
    adx_trend_threshold: float = 25.0
    atr_expansion_multiplier: float = 1.5
    bb_squeeze_percentile: float = 20.0

    # Data provider
    data_provider: str = "yfinance"
    scan_interval_seconds: int = 300

    # 3-hour auto-scan scheduler (main.py): scans src.watchlist_store's
    # dynamic symbol list and only alerts on high-confidence, actively
    # triggered setups — see min_signal_confidence below.
    auto_scan_interval_seconds: int = 10_800  # 3 hours
    min_signal_confidence: float = 0.6
    # Send one aggregate Arabic status after each automated scan cycle.
    send_market_status: bool = True

    # Webhook bridge
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8000
    # Railway injects PORT; local deployments continue to use WEBHOOK_PORT.
    port: int | None = None
    # Optional mounted directory for mutable JSON state on persistent hosts.
    forex_data_dir: Path | None = None
    webhook_shared_secret: str = "change-me-to-a-long-random-string"

    # Notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Optional comma-separated outbound targets (e.g. Telegram group IDs).
    # If empty, outbound alerts continue to use telegram_chat_id.
    telegram_notification_chat_ids: str = ""
    # Optional Telegram user ID allowlist. Required when commands are enabled
    # in a group/supergroup, where a chat ID alone would authorize everyone.
    telegram_allowed_user_id: str = ""
    discord_webhook_url: str = ""
    enable_desktop_notifications: bool = False

    # On-demand Telegram /scan: which configured timeframe actually drives
    # the trade signal; the rest of `timeframes` are shown for context only.
    telegram_primary_timeframe: str = "1h"

    # Paper trading (OANDA v20 practice/demo account only — see
    # src/oanda_client.py, which refuses to run against anything but
    # "practice"). Opt-in: existing alert-only behavior is unaffected
    # unless this is explicitly enabled.
    enable_paper_trading: bool = False
    paper_account_id: str = ""
    paper_api_token: str = ""
    paper_trading_env: str = "practice"

    log_level: str = "INFO"

    @field_validator("watchlist", "timeframes")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v

    @field_validator("account_size_usd", "risk_percent_per_trade", "min_risk_reward")
    @classmethod
    def _positive_finite_risk_settings(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("risk settings must be finite and greater than zero")
        return v

    @field_validator("risk_percent_per_trade")
    @classmethod
    def _risk_percent_not_over_100(cls, v: float) -> float:
        if v > 100:
            raise ValueError("risk_percent_per_trade must not exceed 100")
        return v

    @field_validator("scan_interval_seconds", "auto_scan_interval_seconds")
    @classmethod
    def _positive_intervals(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("scan intervals must be greater than zero")
        return v

    @field_validator("min_signal_confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not math.isfinite(v) or not 0 <= v <= 1:
            raise ValueError("min_signal_confidence must be between 0 and 1")
        return v

    @field_validator("webhook_port")
    @classmethod
    def _valid_webhook_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("webhook_port must be between 1 and 65535")
        return v

    @field_validator("port")
    @classmethod
    def _valid_platform_port(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        return v

    @property
    def service_port(self) -> int:
        return self.port if self.port is not None else self.webhook_port

    @property
    def watchlist_symbols(self) -> list[str]:
        return [s.strip() for s in self.watchlist.split(",") if s.strip()]

    @property
    def timeframe_list(self) -> list[str]:
        return [t.strip() for t in self.timeframes.split(",") if t.strip()]

    @property
    def telegram_notification_targets(self) -> list[str]:
        """Return explicit alert destinations, falling back to the command chat."""
        configured = [
            item.strip()
            for item in self.telegram_notification_chat_ids.split(",")
            if item.strip()
        ]
        if configured:
            return configured
        fallback = self.telegram_chat_id.strip()
        return [fallback] if fallback else []


@lru_cache
def get_settings() -> Settings:
    return Settings()
