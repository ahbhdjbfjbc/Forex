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
from dataclasses import replace

from src.models import RiskParameters, TradeDirection
from src.symbol_resolver import instrument_type

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


def reprice_signal_to_quote(signal, quote, settings, max_entry_drift_r: float = 0.25):
    """Rebase planned levels to a fresh side-specific quote without widening risk.

    Preserves the strategy's price distances, rejects a moved entry beyond a
    quarter of the planned initial risk, and rebuilds position sizing from the
    new entry/stop. It does not execute or guarantee a broker fill.
    """
    old_entry = float(signal.entry_price)
    old_stop = float(signal.stop_loss_price)
    old_risk_distance = abs(old_entry - old_stop)
    if not math.isfinite(old_entry) or not math.isfinite(old_stop) or old_risk_distance <= 0:
        raise ValueError("signal has invalid entry or stop distance")

    direction = signal.direction
    new_entry = float(quote.ask if direction == TradeDirection.BUY else quote.bid)
    delta = new_entry - old_entry
    if abs(delta) > old_risk_distance * max_entry_drift_r:
        raise ValueError("live quote moved more than the allowed fraction of initial risk")

    new_stop = old_stop + delta
    new_tp1 = float(signal.take_profit_1_price) + delta
    new_tp2 = float(signal.take_profit_2_price) + delta
    if direction == TradeDirection.BUY:
        valid = new_stop < new_entry < new_tp1 < new_tp2
    else:
        valid = new_tp2 < new_tp1 < new_entry < new_stop
    if not valid:
        raise ValueError("rebased stop/target levels are not on the correct side of entry")
    if abs(new_entry - new_stop) <= float(quote.spread) * 2:
        raise ValueError("planned stop distance is too small relative to live spread")

    symbol = signal.symbol
    if instrument_type(symbol) == "FX":
        risk_params = build_risk_parameters(
            symbol, settings.account_size_usd, settings.risk_percent_per_trade,
            new_entry, new_stop,
        )
        sl_distance = risk_params.stop_loss_pips
        tp1_distance = round(price_to_pips(symbol, new_tp1 - new_entry), 1)
        tp2_distance = round(price_to_pips(symbol, new_tp2 - new_entry), 1)
    else:
        unit_size = calculate_unit_size(
            settings.account_size_usd, settings.risk_percent_per_trade, new_entry, new_stop
        )
        risk_params = RiskParameters(
            account_size_usd=settings.account_size_usd,
            risk_percent=settings.risk_percent_per_trade,
            stop_loss_pips=round(abs(new_entry - new_stop), 4),
            pip_value_per_lot=0.0,
            lot_size=unit_size,
            risk_amount_usd=round(settings.account_size_usd * settings.risk_percent_per_trade / 100, 2),
        )
        sl_distance = risk_params.stop_loss_pips
        tp1_distance = round(abs(new_tp1 - new_entry), 4)
        tp2_distance = round(abs(new_tp2 - new_entry), 4)

    return replace(
        signal,
        entry_price=new_entry,
        stop_loss_price=round(new_stop, 8),
        stop_loss_pips=sl_distance,
        take_profit_1_price=round(new_tp1, 8),
        take_profit_1_pips=tp1_distance,
        take_profit_1_rr=abs(new_tp1 - new_entry) / abs(new_entry - new_stop),
        take_profit_2_price=round(new_tp2, 8),
        take_profit_2_pips=tp2_distance,
        take_profit_2_rr=abs(new_tp2 - new_entry) / abs(new_entry - new_stop),
        risk=risk_params,
    )
