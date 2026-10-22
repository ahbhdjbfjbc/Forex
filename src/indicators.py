"""Pure technical-indicator math. Stateless: pandas/numpy in, values out.

No fetching, no knowledge of "regime" or "strategy" — those live in
scanner_1_regime.py and scanner_2_strategy.py respectively.
"""
from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _wilder_smooth(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1 / length, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    ranges = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return _wilder_smooth(true_range(df), length)


def adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """Returns a DataFrame with columns ADX, PLUS_DI, MINUS_DI."""
    up_move = df["High"].diff()
    down_move = -df["Low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = _wilder_smooth(true_range(df), length)
    plus_dm_smooth = _wilder_smooth(pd.Series(plus_dm, index=df.index), length)
    minus_dm_smooth = _wilder_smooth(pd.Series(minus_dm, index=df.index), length)

    plus_di = 100 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
    minus_di = 100 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_series = _wilder_smooth(dx.fillna(0), length)

    return pd.DataFrame({"ADX": adx_series, "PLUS_DI": plus_di, "MINUS_DI": minus_di})


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = _wilder_smooth(gain, length)
    avg_loss = _wilder_smooth(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs)).fillna(100)


def stochastic(
    df: pd.DataFrame, k_length: int = 14, k_smooth: int = 3, d_length: int = 3
) -> pd.DataFrame:
    lowest_low = df["Low"].rolling(k_length).min()
    highest_high = df["High"].rolling(k_length).max()
    raw_k = 100 * (df["Close"] - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    k = raw_k.rolling(k_smooth).mean()
    d = k.rolling(d_length).mean()
    return pd.DataFrame({"%K": k, "%D": d})


def bollinger_bands(
    series: pd.Series, length: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    mid = series.rolling(length).mean()
    std = series.rolling(length).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    bandwidth = (upper - lower) / mid.replace(0, np.nan)
    return pd.DataFrame({"MID": mid, "UPPER": upper, "LOWER": lower, "BANDWIDTH": bandwidth})


def bandwidth_percentile_rank(bandwidth: pd.Series, lookback: int = 100) -> float:
    """Percentile rank (0..100) of the latest bandwidth value within the
    trailing `lookback` window. Low values => squeeze; high => expansion."""
    window = bandwidth.dropna().tail(lookback)
    if len(window) < 5:
        return 50.0
    latest = window.iloc[-1]
    return float((window <= latest).mean() * 100)


def donchian_channel(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
    upper = df["High"].rolling(length).max()
    lower = df["Low"].rolling(length).min()
    mid = (upper + lower) / 2
    return pd.DataFrame({"UPPER": upper, "MID": mid, "LOWER": lower})


def keltner_channel(
    df: pd.DataFrame, ema_length: int = 20, atr_length: int = 10, multiplier: float = 2.0
) -> pd.DataFrame:
    mid = ema(df["Close"], ema_length)
    rng = atr(df, atr_length)
    upper = mid + multiplier * rng
    lower = mid - multiplier * rng
    return pd.DataFrame({"UPPER": upper, "MID": mid, "LOWER": lower})


def detect_structure(df: pd.DataFrame, lookback: int = 50, swing_window: int = 5) -> dict:
    """Fractal-based swing high/low detection to flag Higher-Highs /
    Lower-Lows structure over the trailing `lookback` bars."""
    recent = df.tail(lookback)
    highs = recent["High"]
    lows = recent["Low"]

    swing_highs = []
    swing_lows = []
    n = len(recent)
    for i in range(swing_window, n - swing_window):
        window_high = highs.iloc[i - swing_window : i + swing_window + 1]
        if highs.iloc[i] == window_high.max():
            swing_highs.append((recent.index[i], highs.iloc[i]))
        window_low = lows.iloc[i - swing_window : i + swing_window + 1]
        if lows.iloc[i] == window_low.min():
            swing_lows.append((recent.index[i], lows.iloc[i]))

    higher_highs = len(swing_highs) >= 2 and swing_highs[-1][1] > swing_highs[-2][1]
    lower_lows = len(swing_lows) >= 2 and swing_lows[-1][1] < swing_lows[-2][1]
    higher_lows = len(swing_lows) >= 2 and swing_lows[-1][1] > swing_lows[-2][1]
    lower_highs = len(swing_highs) >= 2 and swing_highs[-1][1] < swing_highs[-2][1]

    return {
        "higher_highs": higher_highs,
        "lower_lows": lower_lows,
        "higher_lows": higher_lows,
        "lower_highs": lower_highs,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
    }


def fib_retracement_levels(swing_high: float, swing_low: float) -> dict[str, float]:
    diff = swing_high - swing_low
    return {
        "0.0": swing_high,
        "0.236": swing_high - 0.236 * diff,
        "0.382": swing_high - 0.382 * diff,
        "0.5": swing_high - 0.5 * diff,
        "0.618": swing_high - 0.618 * diff,
        "0.786": swing_high - 0.786 * diff,
        "1.0": swing_low,
    }


def detect_rsi_divergence(
    df: pd.DataFrame, rsi_series: pd.Series, lookback: int = 30, swing_window: int = 3
) -> Literal["bullish", "bearish", "none"]:
    """Compares the two most recent price swing points against RSI at the
    same points. Bullish divergence: price makes a lower low while RSI makes
    a higher low. Bearish divergence: price makes a higher high while RSI
    makes a lower high."""
    recent = df.tail(lookback)
    lows = recent["Low"]
    highs = recent["High"]
    n = len(recent)

    swing_lows = []
    swing_highs = []
    for i in range(swing_window, n - swing_window):
        lo_window = lows.iloc[i - swing_window : i + swing_window + 1]
        if lows.iloc[i] == lo_window.min():
            swing_lows.append(i)
        hi_window = highs.iloc[i - swing_window : i + swing_window + 1]
        if highs.iloc[i] == hi_window.max():
            swing_highs.append(i)

    rsi_recent = rsi_series.tail(lookback).reset_index(drop=True)

    if len(swing_lows) >= 2:
        i1, i2 = swing_lows[-2], swing_lows[-1]
        if lows.iloc[i2] < lows.iloc[i1] and rsi_recent.iloc[i2] > rsi_recent.iloc[i1]:
            return "bullish"

    if len(swing_highs) >= 2:
        i1, i2 = swing_highs[-2], swing_highs[-1]
        if highs.iloc[i2] > highs.iloc[i1] and rsi_recent.iloc[i2] < rsi_recent.iloc[i1]:
            return "bearish"

    return "none"
