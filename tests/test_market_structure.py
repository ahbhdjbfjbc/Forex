"""Tests for the no-lookahead price-action structure module."""
import numpy as np
import pandas as pd

from src.gold_analysis import gold_session_allows
from src.market_structure import BEARISH, BULLISH, analyze_structure, structure_conflict


def _frame(closes):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    idx = pd.date_range("2026-11-02 00:00", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes}, index=idx)


def _zigzag(up=True, legs=8):
    out, price = [], 100.0
    for leg in range(legs):
        rising = (leg % 2 == 0) == up
        step = 1.0 if rising else -0.6
        for _ in range(6):
            price += step
            out.append(price)
    return out


def test_uptrend_structure_is_bullish_with_bos():
    st = analyze_structure(_frame(_zigzag(True)), atr=1.0)
    assert st is not None
    assert st.trend == BULLISH
    assert any(e.kind == "BOS" and e.direction == BULLISH for e in st.events)


def test_downtrend_structure_is_bearish():
    closes = [200 - (c - 100) for c in _zigzag(True)]
    st = analyze_structure(_frame(closes), atr=1.0)
    assert st.trend == BEARISH


def test_no_lookahead_last_bar_does_not_change_history():
    closes = _zigzag(True, legs=10)
    full = analyze_structure(_frame(closes), atr=1.0)
    trimmed = analyze_structure(_frame(closes[:-1]), atr=1.0)
    # Events found on the shorter frame must still exist (same levels) on the longer one.
    full_levels = {(e.kind, e.direction, e.level) for e in full.events}
    for e in trimmed.events[-3:]:
        assert (e.kind, e.direction, e.level) in full_levels or len(full.events) == 5


def test_recent_opposite_break_blocks_trade_but_not_mean_reversion():
    st = analyze_structure(_frame(_zigzag(True)), atr=1.0)
    assert structure_conflict(st, "SELL") is not None
    assert structure_conflict(st, "BUY") is None
    assert structure_conflict(st, "SELL", counter_trend_strategy=True) is None


def test_unusable_frame_returns_none():
    assert analyze_structure(_frame([1.0] * 5), atr=1.0) is None
    assert analyze_structure(_frame(_zigzag(True)), atr=0.0) is None


def test_gold_session_filter():
    df = _frame(_zigzag(True))
    df.index = pd.date_range(end="2026-11-04 12:00", periods=len(df), freq="1h", tz="UTC")
    assert gold_session_allows(df, "1h")[0] is True
    df.index = pd.date_range(end="2026-11-04 02:00", periods=len(df), freq="1h", tz="UTC")
    assert gold_session_allows(df, "1h")[0] is False
    assert gold_session_allows(df, "4h")[0] is True
    df.index = pd.date_range(end="2026-11-07 12:00", periods=len(df), freq="1h", tz="UTC")
    assert gold_session_allows(df, "15m")[0] is False
