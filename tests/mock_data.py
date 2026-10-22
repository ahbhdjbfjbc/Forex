"""Synthetic OHLCV generators — one per `MarketRegime` — so Scanner 1 and
Scanner 2 can be exercised deterministically without live API access.

Each generator is tuned (see tests/test_scanners.py) so that running the
real `src.scanner_1_regime.classify_regime` pipeline over its output lands
in the intended regime bucket.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _frame(index: pd.DatetimeIndex, close: np.ndarray, high_pad: np.ndarray, low_pad: np.ndarray, volume: np.ndarray) -> pd.DataFrame:
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + high_pad
    low = np.minimum(open_, close) - low_pad
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=index
    )


def _index(bars: int, freq: str = "1h") -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=bars, freq=freq)


def make_strong_trend_bullish(bars: int = 300, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = 0.0009
    noise = rng.normal(0, 0.0004, bars)
    returns = drift + noise
    close = 1.1000 * np.exp(np.cumsum(returns))
    pad = np.abs(rng.normal(0, 0.0003, bars))
    volume = rng.normal(1000, 100, bars).clip(min=100)
    return _frame(_index(bars), close, pad, pad, volume)


def make_strong_trend_bearish(bars: int = 300, seed: int = 2) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = -0.0009
    noise = rng.normal(0, 0.0004, bars)
    returns = drift + noise
    close = 1.1000 * np.exp(np.cumsum(returns))
    pad = np.abs(rng.normal(0, 0.0003, bars))
    volume = rng.normal(1000, 100, bars).clip(min=100)
    return _frame(_index(bars), close, pad, pad, volume)


def _mean_reverting(n: int, phi: float, eps_std: float, rng: np.random.Generator) -> np.ndarray:
    x = np.zeros(n)
    eps = rng.normal(0, eps_std, n)
    for i in range(1, n):
        x[i] = x[i - 1] * phi + eps[i]
    return x


def make_ranging_consolidation(bars: int = 300, seed: int = 3) -> pd.DataFrame:
    """Mean-reverting (not sinusoidal) oscillation around a fixed level:
    a smooth sine wave produces multi-bar directional runs long enough to
    spike ADX, whereas an AR(1) mean-reverting process whipsaws bar-to-bar
    and stays non-trending, which is what a real consolidation regime looks
    like. The last third of the series tightens further to approximate a
    volatility squeeze."""
    rng = np.random.default_rng(seed)
    wide_bars, squeeze_bars = bars - 100, 100
    x_wide = _mean_reverting(wide_bars, phi=0.85, eps_std=0.00035, rng=rng)
    x_squeeze = _mean_reverting(squeeze_bars, phi=0.85, eps_std=0.00006, rng=rng)
    x_squeeze = x_squeeze - x_squeeze[0] + x_wide[-1]  # continuity at the seam
    x = np.concatenate([x_wide, x_squeeze])
    close = 1.1000 * (1 + x)
    pad = np.concatenate(
        [np.abs(rng.normal(0, 0.00006, wide_bars)), np.abs(rng.normal(0, 0.00002, squeeze_bars))]
    )
    volume = rng.normal(1000, 50, bars).clip(min=100)
    return _frame(_index(bars), close, pad, pad, volume)


def make_high_volatility_expansion(bars: int = 300, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    quiet_bars = bars - 40
    quiet_noise = rng.normal(0, 0.00015, quiet_bars)
    # Strict up/down alternation during the burst: big ranges (ATR/BB
    # bandwidth expand hard) without a sustained net direction, so this
    # doesn't get mistaken for a strong trend.
    burst_n = bars - quiet_bars
    alt_sign = np.resize([1, -1], burst_n)
    burst_noise = alt_sign * np.abs(rng.normal(0.0028, 0.0004, burst_n))
    returns = np.concatenate([quiet_noise, burst_noise])
    close = 1.1000 * np.exp(np.cumsum(returns))
    pad = np.concatenate(
        [np.abs(rng.normal(0, 0.00008, quiet_bars)), np.abs(rng.normal(0.0018, 0.0006, burst_n))]
    )
    volume = np.concatenate(
        [rng.normal(900, 50, quiet_bars), rng.normal(2600, 300, burst_n)]
    ).clip(min=100)
    return _frame(_index(bars), close, pad, pad, volume)


def make_low_liquidity_chop(bars: int = 300, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.0003, bars)
    close = 1.1000 * np.exp(np.cumsum(noise))
    pad = np.abs(rng.normal(0, 0.00015, bars))
    volume = rng.normal(1000, 80, bars).clip(min=100)
    # Only the final bar goes dead — the trailing baseline (bars before it)
    # stays normal, so RVOL genuinely collapses instead of dragging its own
    # average down with it.
    volume[-1] = 15.0
    return _frame(_index(bars), close, pad, pad, volume)


REGIME_GENERATORS = {
    "STRONG_TREND_BULLISH": make_strong_trend_bullish,
    "STRONG_TREND_BEARISH": make_strong_trend_bearish,
    "RANGING_CONSOLIDATION": make_ranging_consolidation,
    "HIGH_VOLATILITY_EXPANSION": make_high_volatility_expansion,
    "LOW_LIQUIDITY_CHOP": make_low_liquidity_chop,
}


# ---------------------------------------------------------------------------
# Entry-trigger variants — regime data engineered so Scanner 2's specific
# strategy condition (EMA20 touch, BB/RSI extreme, Donchian breakout) is
# actually active on the final bar, not just the regime label.
# ---------------------------------------------------------------------------


def _force_ema20_touch(df: pd.DataFrame) -> pd.DataFrame:
    """Widen the final bar's High/Low so it brackets the just-computed EMA20,
    guaranteeing a 'touch' regardless of how the hand-tuned drift landed."""
    from src import indicators as ind

    df = df.copy()
    ema20_last = ind.ema(df["Close"], 20).iloc[-1]
    o, c = df["Open"].iloc[-1], df["Close"].iloc[-1]
    needed_pad = abs(ema20_last - c) * 1.25 + 0.00005
    df.iloc[-1, df.columns.get_loc("High")] = max(o, c, ema20_last) + needed_pad
    df.iloc[-1, df.columns.get_loc("Low")] = min(o, c, ema20_last) - needed_pad
    return df


def make_trend_pullback_bullish(bars: int = 300, seed: int = 1, pullback_bars: int = 13) -> pd.DataFrame:
    """STRONG_TREND_BULLISH regime with an active pullback entry: price dips
    toward EMA20 over `pullback_bars`, then the final bar ticks back up
    (RSI turning up from below 55) while bracketing EMA20."""
    df = make_strong_trend_bullish(bars=bars, seed=seed).copy()
    close = df["Close"].values.copy()
    rng = np.random.default_rng(seed + 100)
    last_before = close[-pullback_bars - 1]
    pb_returns = np.full(pullback_bars - 1, -0.0007) + rng.normal(0, 0.00004, pullback_bars - 1)
    close[-pullback_bars:-1] = last_before * np.exp(np.cumsum(pb_returns))
    close[-1] = close[-2] * np.exp(0.0009)
    df["Close"] = close
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    df["Open"] = open_
    pad = np.abs(rng.normal(0, 0.00015, bars))
    df["High"] = np.maximum(df["Open"], df["Close"]) + pad
    df["Low"] = np.minimum(df["Open"], df["Close"]) - pad
    return _force_ema20_touch(df)


def make_trend_pullback_bearish(bars: int = 300, seed: int = 2, pullback_bars: int = 13) -> pd.DataFrame:
    """Mirror of `make_trend_pullback_bullish` for STRONG_TREND_BEARISH:
    price bounces up toward EMA20, then the final bar ticks back down
    (RSI turning down from above 45)."""
    df = make_strong_trend_bearish(bars=bars, seed=seed).copy()
    close = df["Close"].values.copy()
    rng = np.random.default_rng(seed + 100)
    last_before = close[-pullback_bars - 1]
    pb_returns = np.full(pullback_bars - 1, 0.0007) + rng.normal(0, 0.00004, pullback_bars - 1)
    close[-pullback_bars:-1] = last_before * np.exp(np.cumsum(pb_returns))
    close[-1] = close[-2] * np.exp(-0.0009)
    df["Close"] = close
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    df["Open"] = open_
    pad = np.abs(rng.normal(0, 0.00015, bars))
    df["High"] = np.maximum(df["Open"], df["Close"]) + pad
    df["Low"] = np.minimum(df["Open"], df["Close"]) - pad
    return _force_ema20_touch(df)


def make_mean_reversion_overbought(bars: int = 300, seed: int = 3) -> pd.DataFrame:
    """RANGING_CONSOLIDATION-shaped data with the final bar poking above the
    upper Bollinger Band while RSI is overbought — the SELL fade trigger.
    Note: this single-bar spike also pushes ADX/ATR up enough that Scanner
    1's own classifier would call it HIGH_VOLATILITY_EXPANSION; the test
    forces regime=RANGING_CONSOLIDATION deliberately, to exercise Scanner
    2's fade logic in isolation from Scanner 1's classification."""
    from src import indicators as ind

    df = make_ranging_consolidation(bars=bars, seed=seed).copy()
    bb = ind.bollinger_bands(df["Close"], 20)
    upper = bb["UPPER"].iloc[-1]
    o = df["Open"].iloc[-1]
    df.iloc[-1, df.columns.get_loc("Close")] = upper * 1.001
    c = df["Close"].iloc[-1]
    df.iloc[-1, df.columns.get_loc("High")] = max(o, c) + 0.0001
    df.iloc[-1, df.columns.get_loc("Low")] = min(o, c) - 0.0001
    return df


def make_mean_reversion_oversold(bars: int = 300, seed: int = 3) -> pd.DataFrame:
    """Mirror of `make_mean_reversion_overbought`: pokes below the lower BB
    with RSI oversold, the BUY fade trigger."""
    from src import indicators as ind

    df = make_ranging_consolidation(bars=bars, seed=seed).copy()
    bb = ind.bollinger_bands(df["Close"], 20)
    lower = bb["LOWER"].iloc[-1]
    o = df["Open"].iloc[-1]
    df.iloc[-1, df.columns.get_loc("Close")] = lower * 0.999
    c = df["Close"].iloc[-1]
    df.iloc[-1, df.columns.get_loc("High")] = max(o, c) + 0.0001
    df.iloc[-1, df.columns.get_loc("Low")] = min(o, c) - 0.0001
    return df


def make_volatility_breakout_long(bars: int = 300, seed: int = 4) -> pd.DataFrame:
    """HIGH_VOLATILITY_EXPANSION-shaped data where the final bar closes above
    the prior bar's 20-period Donchian upper channel with RVOL expansion —
    the volatility-breakout LONG trigger."""
    from src import indicators as ind

    df = make_high_volatility_expansion(bars=bars, seed=seed).copy()
    donchian = ind.donchian_channel(df, 20)
    prior_upper = donchian["UPPER"].iloc[-2]
    o = df["Open"].iloc[-1]
    df.iloc[-1, df.columns.get_loc("Close")] = prior_upper * 1.002
    c = df["Close"].iloc[-1]
    df.iloc[-1, df.columns.get_loc("High")] = max(o, c) + 0.0002
    df.iloc[-1, df.columns.get_loc("Low")] = min(o, c) - 0.0002
    df.iloc[-1, df.columns.get_loc("Volume")] = df["Volume"].iloc[-21:-1].mean() * 1.8
    return df


def make_volatility_breakout_short(bars: int = 300, seed: int = 4) -> pd.DataFrame:
    """Mirror of `make_volatility_breakout_long`: closes below the prior
    bar's Donchian lower channel, the breakout SHORT trigger."""
    from src import indicators as ind

    df = make_high_volatility_expansion(bars=bars, seed=seed).copy()
    donchian = ind.donchian_channel(df, 20)
    prior_lower = donchian["LOWER"].iloc[-2]
    o = df["Open"].iloc[-1]
    df.iloc[-1, df.columns.get_loc("Close")] = prior_lower * 0.998
    c = df["Close"].iloc[-1]
    df.iloc[-1, df.columns.get_loc("High")] = max(o, c) + 0.0002
    df.iloc[-1, df.columns.get_loc("Low")] = min(o, c) - 0.0002
    df.iloc[-1, df.columns.get_loc("Volume")] = df["Volume"].iloc[-21:-1].mean() * 1.8
    return df
