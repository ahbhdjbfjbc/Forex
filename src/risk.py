"""Pip math, lot sizing, and risk/reward calculations.

Known simplification: pip value is converted to USD using the pair's own
last price when the quote currency isn't USD (exact for USD-quoted majors
and for USDJPY; an approximation for cross pairs like EURJPY or EURGBP,
which technically need a separate quote-currency/USD rate). Flagged here
rather than silently assumed — swap in a real conversion feed if cross-pair
lot sizing needs to be exact.
"""
from __future__ import annotations

import math

from src.models import RiskParameters, TradeDirection

STANDARD_LOT_UNITS = 100_000


def pip_size(symbol: str) -> float:
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def price_to_pips(symbol: str, price_diff: float) -> float:
    return abs(price_diff) / pip_size(symbol)


def pip_value_per_lot(symbol: str, price: float, lot_units: float = STANDARD_LOT_UNITS) -> float:
    """USD value of a 1-pip move for `lot_units` units of `symbol`."""
    quote_ccy = symbol.upper().replace("=X", "")[-3:]
    raw_pip_value = pip_size(symbol) * lot_units
    if quote_ccy == "USD":
        return raw_pip_value
    if price <= 0:
        return 0.0
    return raw_pip_value / price


def calculate_lot_size(
    account_size_usd: float,
    risk_percent: float,
    stop_loss_pips: float,
    pip_value_per_standard_lot: float,
) -> float:
    inputs = (account_size_usd, risk_percent, stop_loss_pips, pip_value_per_standard_lot)
    if not all(math.isfinite(value) for value in inputs):
        return 0.0
    if account_size_usd <= 0 or not 0 <= risk_percent <= 100:
        return 0.0
    if stop_loss_pips <= 0 or pip_value_per_standard_lot <= 0:
        return 0.0

    risk_amount = account_size_usd * (risk_percent / 100)
    lots = risk_amount / (stop_loss_pips * pip_value_per_standard_lot)
    if not math.isfinite(lots) or lots <= 0:
        return 0.0
    # Standard FX lots commonly trade in 0.01 increments. Round down, not to
    # nearest, so discretization cannot raise position risk above the budget.
    return math.floor(lots * 100 + 1e-9) / 100


def compute_rr_targets(
    entry_price: float,
    stop_loss_price: float,
    direction: TradeDirection,
    rr_tp1: float = 2.0,
    rr_tp2: float = 3.0,
) -> tuple[float, float]:
    """Returns (tp1_price, tp2_price) given entry/SL and target R:R multiples."""
    risk_distance = abs(entry_price - stop_loss_price)
    if direction == TradeDirection.BUY:
        return entry_price + risk_distance * rr_tp1, entry_price + risk_distance * rr_tp2
    return entry_price - risk_distance * rr_tp1, entry_price - risk_distance * rr_tp2


def build_risk_parameters(
    symbol: str,
    account_size_usd: float,
    risk_percent: float,
    entry_price: float,
    stop_loss_price: float,
) -> RiskParameters:
    sl_pips = price_to_pips(symbol, entry_price - stop_loss_price)
    pip_val = pip_value_per_lot(symbol, entry_price)
    lots = calculate_lot_size(account_size_usd, risk_percent, sl_pips, pip_val)
    return RiskParameters(
        account_size_usd=account_size_usd,
        risk_percent=risk_percent,
        stop_loss_pips=round(sl_pips, 1),
        pip_value_per_lot=round(pip_val, 4),
        lot_size=lots,
        risk_amount_usd=round(account_size_usd * (risk_percent / 100), 2),
    )


def calculate_unit_size(
    account_size_usd: float,
    risk_percent: float,
    entry_price: float,
    stop_loss_price: float,
) -> float:
    """Risk-based position size in raw units of the instrument (ounces,
    coins, ...) rather than FX lots. This is what "lots" reduces to anyway
    for a USD-quoted instrument (risk_amount / price_distance), so it's the
    right primitive for metals/crypto where a standard 100k-unit "lot"
    convention doesn't apply the way it does in FX."""
    distance = abs(entry_price - stop_loss_price)
    if distance <= 0:
        return 0.0
    risk_amount = account_size_usd * (risk_percent / 100)
    return round(risk_amount / distance, 4)
