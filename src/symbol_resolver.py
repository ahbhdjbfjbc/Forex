"""Free-text ticker parsing for on-demand requests (Telegram, future channels).

Owns: normalizing whatever a human types ("gold", "eur/usd", "xauusd") into
a canonical instrument symbol, and mapping that canonical symbol to the
concrete ticker string the data provider needs. Nothing here fetches data
or knows about regimes/strategies.

Known simplification: Yahoo Finance has no retail spot ticker for metals
(no "XAUUSD=X"), so gold/silver are proxied via COMEX futures (GC=F, SI=F).
These track spot closely but aren't identical — fine for regime/strategy
scanning, not for precise arbitrage-level pricing.
"""
from __future__ import annotations

import re
from typing import Literal

InstrumentType = Literal["FX", "METAL", "CRYPTO"]

_ALIASES: dict[str, str] = {
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "CABLE": "GBPUSD",
    "FIBER": "EURUSD",
    "LOONIE": "USDCAD",
    "AUSSIE": "AUDUSD",
    "KIWI": "NZDUSD",
    "SWISSY": "USDCHF",
    "SWISSIE": "USDCHF",
    "BITCOIN": "BTCUSD",
    "ETHEREUM": "ETHUSD",
}

_CRYPTO_BASES = {"BTC", "ETH", "LTC", "XRP", "DOGE", "SOL", "ADA", "BNB", "DOT", "AVAX"}
_METALS = {"XAUUSD", "XAGUSD"}

_VALID_SYMBOL_RE = re.compile(r"^[A-Z]{6,7}$")


def sanitize_symbol(raw: str) -> str | None:
    """Normalize free-text input to a canonical 6-7 letter symbol
    (e.g. "eur/usd" -> "EURUSD", "gold" -> "XAUUSD"). Returns None if the
    cleaned text doesn't look like a tradeable symbol at all."""
    if not raw:
        return None
    text = raw.strip().upper()
    text = re.sub(r"[^A-Z]", "", text)  # drop slashes, spaces, punctuation, digits
    text = _ALIASES.get(text, text)
    if not _VALID_SYMBOL_RE.fullmatch(text):
        return None
    return text


def instrument_type(symbol: str) -> InstrumentType:
    """Accepts either a canonical symbol ("XAUUSD") or a yfinance-ready
    ticker ("GC=F", "BTC-USD", "EURUSD=X") — normalizes both the same way
    before classifying, since callers pass either depending on context."""
    s = symbol.upper().replace("=X", "").replace("=F", "").replace("-", "")
    if s in _METALS or s in {"GC", "SI"}:
        return "METAL"
    if any(s.startswith(base) and s[len(base):] in {"USD", "USDT"} for base in _CRYPTO_BASES):
        return "CRYPTO"
    return "FX"


def to_yfinance_ticker(symbol: str) -> str:
    """Canonical symbol -> the ticker string `src.data_provider` should fetch."""
    if symbol == "XAUUSD":
        return "GC=F"
    if symbol == "XAGUSD":
        return "SI=F"
    kind = instrument_type(symbol)
    if kind == "CRYPTO":
        for base in _CRYPTO_BASES:
            if symbol.startswith(base):
                return f"{base}-USD"
    return f"{symbol}=X"


def display_pair(symbol: str) -> str:
    """Canonical symbol -> human-readable "EUR/USD" style label."""
    if len(symbol) == 6:
        return f"{symbol[:3]}/{symbol[3:]}"
    return symbol
