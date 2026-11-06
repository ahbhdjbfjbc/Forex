"""One price formatter shared by the Telegram text and the chart image, so the
numbers a subscriber reads in the caption are exactly the numbers on the chart."""
from __future__ import annotations

import math

from src.symbol_resolver import instrument_type


def price_decimals(symbol: str) -> int:
    kind = instrument_type(str(symbol))
    if kind == "FX":
        return 3 if "JPY" in str(symbol).upper() else 5
    return 2  # metals and crypto are quoted to cents


def format_price(value: float, symbol: str) -> str:
    value = float(value)
    if not math.isfinite(value):
        return "—"
    return f"{value:.{price_decimals(symbol)}f}"


def round_price(value: float, symbol: str) -> float:
    return round(float(value), price_decimals(symbol))
