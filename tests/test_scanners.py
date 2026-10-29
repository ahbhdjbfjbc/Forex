"""Dry-run validation for Scanner 1 (regime) and Scanner 2 (strategy), plus
focused unit tests for the supporting modules (risk, session/RVOL, notifier
formatting, and the TradingView webhook bridge). Everything here runs on
synthetic data from `tests/mock_data.py` — no network access required.

Run with: pytest tests/ -v
"""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from config import Settings
from src import risk
from src import session_utils as sess
from src.models import (
    MarketRegime,
    RegimeResult,
    StrategyType,
    StructuralMetrics,
    TradeDirection,
    ForexSession,
)
from src.notifier import format_message
from src.scanner_1_regime import _build_metrics, classify_regime, scan_symbols_with_data
from src.scanner_2_strategy import generate_signal
from src.symbol_resolver import (
    display_pair,
    instrument_type,
    sanitize_symbol,
    to_yfinance_ticker,
)
from src.telegram_bot import run_on_demand_scan
from src.tradingview_bridge import app as bridge_app
from src.watchlist_store import DEFAULT_WATCHLIST, WatchlistStore
from tests import mock_data as md


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        adx_trend_threshold=25.0,
        atr_expansion_multiplier=1.5,
        bb_squeeze_percentile=20.0,
        account_size_usd=10_000.0,
        risk_percent_per_trade=1.0,
        min_risk_reward=2.0,
        webhook_shared_secret="test-secret",
    )


def _base_metrics(**overrides) -> StructuralMetrics:
    defaults = dict(
        adx=10.0, ema_20=1.10, ema_50=1.10, ema_200=1.10,
        higher_highs=False, lower_lows=False, atr=0.001, atr_avg=0.001,
        bb_bandwidth=0.01, bb_bandwidth_percentile=50.0, rvol=1.0,
        session=ForexSession.LONDON, close=1.10,
    )
    defaults.update(overrides)
    return StructuralMetrics(**defaults)


# ---------------------------------------------------------------------------
# Scanner 1 — classify_regime unit tests (direct rule coverage)
# ---------------------------------------------------------------------------


class TestClassifyRegimeRules:
    def test_strong_trend_bullish(self, settings):
        metrics = _base_metrics(adx=30, ema_20=1.12, ema_50=1.10, ema_200=1.08, higher_highs=True)
        regime, conf = classify_regime(metrics, settings)
        assert regime == MarketRegime.STRONG_TREND_BULLISH
        assert conf > 0.5

    def test_strong_trend_bearish(self, settings):
        metrics = _base_metrics(adx=30, ema_20=1.08, ema_50=1.10, ema_200=1.12, lower_lows=True)
        regime, conf = classify_regime(metrics, settings)
        assert regime == MarketRegime.STRONG_TREND_BEARISH
        assert conf > 0.5

    def test_high_volatility_expansion_via_atr(self, settings):
        metrics = _base_metrics(adx=10, atr=0.002, atr_avg=0.001, bb_bandwidth_percentile=50.0)
        regime, _ = classify_regime(metrics, settings)
        assert regime == MarketRegime.HIGH_VOLATILITY_EXPANSION

    def test_high_volatility_expansion_via_bandwidth(self, settings):
        metrics = _base_metrics(adx=10, atr=0.001, atr_avg=0.001, bb_bandwidth_percentile=85.0)
        regime, _ = classify_regime(metrics, settings)
        assert regime == MarketRegime.HIGH_VOLATILITY_EXPANSION

    def test_ranging_consolidation_squeeze(self, settings):
        metrics = _base_metrics(adx=10, atr=0.001, atr_avg=0.001, bb_bandwidth_percentile=10.0)
        regime, _ = classify_regime(metrics, settings)
        assert regime == MarketRegime.RANGING_CONSOLIDATION

    def test_low_liquidity_chop(self, settings):
        metrics = _base_metrics(adx=10, atr=0.001, atr_avg=0.001, bb_bandwidth_percentile=50.0, rvol=0.2)
        regime, _ = classify_regime(metrics, settings)
        assert regime == MarketRegime.LOW_LIQUIDITY_CHOP

    def test_trend_beats_volatility_when_both_present(self, settings):
        """A strong trend with expanding ATR should still classify as the
        trend, not volatility expansion — trend takes priority."""
        metrics = _base_metrics(
            adx=35, ema_20=1.12, ema_50=1.10, ema_200=1.08, higher_highs=True,
            atr=0.003, atr_avg=0.001,
        )
        regime, _ = classify_regime(metrics, settings)
        assert regime == MarketRegime.STRONG_TREND_BULLISH


# ---------------------------------------------------------------------------
# Scanner 1 — full pipeline over synthetic OHLCV (data -> indicators -> regime)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expected_regime,generator", list(md.REGIME_GENERATORS.items()))
def test_scan_regime_pipeline_matches_expected_regime(expected_regime, generator, settings):
    df = generator()
    metrics = _build_metrics(df)
    regime, confidence = classify_regime(metrics, settings)
    assert regime.value == expected_regime
    assert 0.0 <= confidence <= 1.0


# ---------------------------------------------------------------------------
# Scanner 2 — strategy mapping & signal generation
# ---------------------------------------------------------------------------


class TestGenerateSignal:
    def test_low_liquidity_chop_is_no_trade(self, settings):
        df = md.make_low_liquidity_chop()
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
        assert regime == MarketRegime.LOW_LIQUIDITY_CHOP  # sanity: fixture actually produces this
        result = RegimeResult("EURUSD=X", "1h", regime, metrics, confidence)
        assert generate_signal(result, df, settings) is None

    def test_trend_pullback_bullish_produces_buy(self, settings):
        df = md.make_trend_pullback_bullish()
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
        assert regime == MarketRegime.STRONG_TREND_BULLISH
        result = RegimeResult("EURUSD=X", "1h", regime, metrics, confidence)
        signal = generate_signal(result, df, settings)
        assert signal is not None
        assert signal.direction == TradeDirection.BUY
        assert signal.strategy == StrategyType.TREND_PULLBACK
        assert signal.stop_loss_price < signal.entry_price < signal.take_profit_1_price
        assert signal.take_profit_1_rr >= settings.min_risk_reward

    def test_trend_pullback_bearish_produces_sell(self, settings):
        df = md.make_trend_pullback_bearish()
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
        assert regime == MarketRegime.STRONG_TREND_BEARISH
        result = RegimeResult("EURUSD=X", "1h", regime, metrics, confidence)
        signal = generate_signal(result, df, settings)
        assert signal is not None
        assert signal.direction == TradeDirection.SELL
        assert signal.strategy == StrategyType.TREND_PULLBACK
        assert signal.take_profit_1_price < signal.entry_price < signal.stop_loss_price

    def test_trend_regime_without_pullback_trigger_returns_none(self, settings):
        """A pure, undisturbed uptrend (no pullback into EMA20) should not
        fire a trend-pullback entry every single bar."""
        df = md.make_strong_trend_bullish()
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
        result = RegimeResult("EURUSD=X", "1h", regime, metrics, confidence)
        assert generate_signal(result, df, settings) is None

    def test_mean_reversion_overbought_produces_sell(self, settings):
        df = md.make_mean_reversion_overbought()
        metrics = _build_metrics(df)
        _, confidence = classify_regime(metrics, settings)
        forced = RegimeResult("EURUSD=X", "1h", MarketRegime.RANGING_CONSOLIDATION, metrics, confidence)
        signal = generate_signal(forced, df, settings)
        assert signal is not None
        assert signal.direction == TradeDirection.SELL
        assert signal.strategy == StrategyType.MEAN_REVERSION_FADE
        assert signal.take_profit_1_rr >= settings.min_risk_reward

    def test_mean_reversion_oversold_produces_buy(self, settings):
        df = md.make_mean_reversion_oversold()
        metrics = _build_metrics(df)
        _, confidence = classify_regime(metrics, settings)
        forced = RegimeResult("EURUSD=X", "1h", MarketRegime.RANGING_CONSOLIDATION, metrics, confidence)
        signal = generate_signal(forced, df, settings)
        assert signal is not None
        assert signal.direction == TradeDirection.BUY
        assert signal.strategy == StrategyType.MEAN_REVERSION_FADE

    def test_volatility_breakout_long(self, settings):
        df = md.make_volatility_breakout_long()
        metrics = _build_metrics(df)
        _, confidence = classify_regime(metrics, settings)
        forced = RegimeResult("EURUSD=X", "1h", MarketRegime.HIGH_VOLATILITY_EXPANSION, metrics, confidence)
        signal = generate_signal(forced, df, settings)
        assert signal is not None
        assert signal.direction == TradeDirection.BUY
        assert signal.strategy == StrategyType.VOLATILITY_BREAKOUT

    def test_volatility_breakout_short(self, settings):
        df = md.make_volatility_breakout_short()
        metrics = _build_metrics(df)
        _, confidence = classify_regime(metrics, settings)
        forced = RegimeResult("EURUSD=X", "1h", MarketRegime.HIGH_VOLATILITY_EXPANSION, metrics, confidence)
        signal = generate_signal(forced, df, settings)
        assert signal is not None
        assert signal.direction == TradeDirection.SELL
        assert signal.strategy == StrategyType.VOLATILITY_BREAKOUT

    def test_all_signals_respect_minimum_risk_reward(self, settings):
        cases = [
            (md.make_trend_pullback_bullish(), MarketRegime.STRONG_TREND_BULLISH),
            (md.make_mean_reversion_overbought(), MarketRegime.RANGING_CONSOLIDATION),
            (md.make_volatility_breakout_long(), MarketRegime.HIGH_VOLATILITY_EXPANSION),
        ]
        for df, forced_regime in cases:
            metrics = _build_metrics(df)
            _, confidence = classify_regime(metrics, settings)
            result = RegimeResult("EURUSD=X", "1h", forced_regime, metrics, confidence)
            signal = generate_signal(result, df, settings)
            assert signal is not None
            assert signal.take_profit_1_rr >= settings.min_risk_reward
            assert signal.take_profit_2_rr > signal.take_profit_1_rr
            assert signal.risk.lot_size >= 0


# ---------------------------------------------------------------------------
# src/risk.py
# ---------------------------------------------------------------------------


class TestRisk:
    def test_pip_size_jpy_vs_non_jpy(self):
        assert risk.pip_size("USDJPY=X") == 0.01
        assert risk.pip_size("EURUSD=X") == 0.0001

    def test_pip_value_usd_quoted_pair(self):
        # EURUSD: quote currency is USD, so 1 std lot = $10/pip exactly.
        assert risk.pip_value_per_lot("EURUSD=X", price=1.10) == pytest.approx(10.0)

    def test_calculate_lot_size_basic(self):
        # $10k account, 1% risk = $100 risk; 50 pip SL, $10/pip/lot -> 0.2 lots.
        lots = risk.calculate_lot_size(10_000, 1.0, 50, 10.0)
        assert lots == pytest.approx(0.20)

    def test_calculate_lot_size_zero_stop_returns_zero(self):
        assert risk.calculate_lot_size(10_000, 1.0, 0, 10.0) == 0.0

    def test_calculate_lot_size_rounds_down_to_avoid_exceeding_risk_budget(self):
        # Raw size = 0.006 lots. Rounding to nearest would increase it to
        # 0.01 and exceed the allowed risk; conservative sizing returns 0.00.
        assert risk.calculate_lot_size(600, 1.0, 100, 10.0) == 0.0

    @pytest.mark.parametrize("values", [
        (float("nan"), 1.0, 50.0, 10.0),
        (10_000.0, 101.0, 50.0, 10.0),
        (10_000.0, -1.0, 50.0, 10.0),
        (10_000.0, 1.0, 50.0, float("inf")),
    ])
    def test_calculate_lot_size_rejects_invalid_inputs(self, values):
        assert risk.calculate_lot_size(*values) == 0.0

    def test_compute_rr_targets_buy(self):
        tp1, tp2 = risk.compute_rr_targets(1.1000, 1.0980, TradeDirection.BUY, rr_tp1=2.0, rr_tp2=3.0)
        assert tp1 == pytest.approx(1.1040)
        assert tp2 == pytest.approx(1.1060)

    def test_compute_rr_targets_sell(self):
        tp1, tp2 = risk.compute_rr_targets(1.1000, 1.1020, TradeDirection.SELL, rr_tp1=2.0, rr_tp2=3.0)
        assert tp1 == pytest.approx(1.0960)
        assert tp2 == pytest.approx(1.0940)

    def test_build_risk_parameters_end_to_end(self):
        params = risk.build_risk_parameters("EURUSD=X", 10_000, 1.0, 1.0850, 1.0820)
        assert params.stop_loss_pips == pytest.approx(30.0, abs=0.1)
        assert params.risk_amount_usd == pytest.approx(100.0)
        assert params.lot_size > 0


# ---------------------------------------------------------------------------
# src/session_utils.py
# ---------------------------------------------------------------------------


class TestSessionUtils:
    def test_weekend_is_off_hours(self):
        import datetime as dt

        saturday = dt.datetime(2024, 1, 6, 12, 0, tzinfo=dt.timezone.utc)
        assert sess.get_forex_session(saturday) == ForexSession.OFF_HOURS

    def test_london_ny_overlap(self):
        import datetime as dt

        t = dt.datetime(2024, 1, 3, 14, 0, tzinfo=dt.timezone.utc)
        assert sess.get_forex_session(t) == ForexSession.LONDON_NY_OVERLAP

    def test_rvol_neutral_on_insufficient_data(self):
        import pandas as pd

        assert sess.compute_rvol(pd.Series([100, 110])) == 1.0

    def test_rvol_collapses_on_dead_final_bar(self):
        import pandas as pd

        volume = pd.Series([1000.0] * 25 + [10.0])
        rvol = sess.compute_rvol(volume, lookback=20)
        assert rvol < 0.5


# ---------------------------------------------------------------------------
# src/notifier.py — message formatting
# ---------------------------------------------------------------------------


class TestNotifierFormatting:
    def test_format_message_contains_required_fields(self, settings):
        df = md.make_trend_pullback_bullish()
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
        result = RegimeResult("EURUSD=X", "1h", regime, metrics, confidence)
        signal = generate_signal(result, df, settings)
        assert signal is not None

        message = format_message(signal)
        assert "توصية تداول جديدة" in message
        assert "الزوج: EUR/USD" in message
        assert "الاتجاه: شراء" in message
        assert "سعر الدخول:" in message
        assert "وقف الخسارة:" in message
        assert "الهدف الأول:" in message
        assert "الهدف الثاني:" in message
        assert "حجم الصفقة المقترح" in message
        assert "الوقت:" in message


# ---------------------------------------------------------------------------
# src/tradingview_bridge.py — webhook HTTP contract
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch) -> TestClient:
    from config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "test-secret-which-is-longer-than-32-chars")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()
    return TestClient(bridge_app)


class TestTradingViewBridge:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_valid_alert_accepted(self, client, monkeypatch):
        monkeypatch.setattr("src.tradingview_bridge.send_alert", lambda signal: {
            "telegram": True, "discord": False, "desktop": False,
        })
        payload = {
            "ticker": "EURUSD",
            "timeframe": "15m",
            "signal": "BUY",
            "price": 1.0850,
            "secret": "test-secret-which-is-longer-than-32-chars",
        }
        resp = client.post("/webhook/tradingview", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_wrong_secret_rejected(self, client):
        payload = {
            "ticker": "EURUSD", "timeframe": "15m", "signal": "BUY",
            "price": 1.0850, "secret": "wrong-secret",
        }
        resp = client.post("/webhook/tradingview", json=payload)
        assert resp.status_code == 403

    def test_malformed_payload_rejected(self, client):
        payload = {"ticker": "EURUSD", "signal": "SIDEWAYS", "price": -1}
        resp = client.post("/webhook/tradingview", json=payload)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# src/symbol_resolver.py
# ---------------------------------------------------------------------------


class TestSymbolResolver:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("EURUSD", "EURUSD"),
            ("eur/usd", "EURUSD"),
            ("  gbpjpy  ", "GBPJPY"),
            ("gold", "XAUUSD"),
            ("SILVER", "XAGUSD"),
            ("cable", "GBPUSD"),
            ("fiber", "EURUSD"),
            ("btc/usd", "BTCUSD"),
            ("bitcoin", "BTCUSD"),
            ("ethereum", "ETHUSD"),
            ("xyz", None),
            ("", None),
            ("12345", None),
        ],
    )
    def test_sanitize_symbol(self, raw, expected):
        assert sanitize_symbol(raw) == expected

    def test_to_yfinance_ticker_fx(self):
        assert to_yfinance_ticker("EURUSD") == "EURUSD=X"

    def test_to_yfinance_ticker_metals(self):
        assert to_yfinance_ticker("XAUUSD") == "GC=F"
        assert to_yfinance_ticker("XAGUSD") == "SI=F"

    def test_to_yfinance_ticker_crypto(self):
        assert to_yfinance_ticker("BTCUSD") == "BTC-USD"
        assert to_yfinance_ticker("ETHUSD") == "ETH-USD"

    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("EURUSD", "FX"),
            ("EURUSD=X", "FX"),
            ("XAUUSD", "METAL"),
            ("GC=F", "METAL"),
            ("SI=F", "METAL"),
            ("BTCUSD", "CRYPTO"),
            ("BTC-USD", "CRYPTO"),
        ],
    )
    def test_instrument_type(self, symbol, expected):
        assert instrument_type(symbol) == expected

    def test_display_pair(self):
        assert display_pair("EURUSD") == "EUR/USD"
        assert display_pair("XAUUSD") == "XAU/USD"


# ---------------------------------------------------------------------------
# scanner_2 risk sizing must branch correctly by instrument type
# ---------------------------------------------------------------------------


class TestInstrumentAwareSignals:
    def test_fx_signal_keeps_pip_and_lot_fields(self, settings):
        df = md.make_trend_pullback_bullish()
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
        result = RegimeResult("EURUSD=X", "1h", regime, metrics, confidence)
        signal = generate_signal(result, df, settings)
        assert signal is not None
        assert instrument_type(signal.symbol) == "FX"
        assert signal.risk.pip_value_per_lot > 0
        # a real FX stop distance in pips is a small double-digit-to-low-
        # hundreds number, not a raw 0.00xx price difference
        assert signal.stop_loss_pips > 1

    def test_metal_signal_uses_unit_sizing_not_fx_pips(self, settings):
        df = md.make_trend_pullback_bullish()  # regime/trigger shape only
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
        result = RegimeResult("XAUUSD", "1h", regime, metrics, confidence)
        signal = generate_signal(result, df, settings)
        assert signal is not None
        assert instrument_type(signal.symbol) == "METAL"
        assert signal.risk.pip_value_per_lot == 0.0
        # stop_loss_pips is repurposed to carry the raw price distance here
        assert signal.stop_loss_pips == pytest.approx(abs(signal.entry_price - signal.stop_loss_price), abs=1e-4)

    def test_crypto_signal_unit_size_matches_manual_calc(self, settings):
        df = md.make_volatility_breakout_long()
        metrics = _build_metrics(df)
        result = RegimeResult("BTCUSD", "1h", MarketRegime.HIGH_VOLATILITY_EXPANSION, metrics, 0.8)
        signal = generate_signal(result, df, settings)
        assert signal is not None
        # entry/stop on the signal are rounded to 5dp for display, while
        # risk.lot_size was computed from the unrounded values internally —
        # recomputing from the rounded numbers should still land close.
        expected_units = risk.calculate_unit_size(
            settings.account_size_usd, settings.risk_percent_per_trade,
            signal.entry_price, signal.stop_loss_price,
        )
        assert signal.risk.lot_size == pytest.approx(expected_units, rel=1e-2)


# ---------------------------------------------------------------------------
# src/telegram_bot.py — on-demand scan orchestration (network calls mocked)
# ---------------------------------------------------------------------------


class TestOnDemandScan:
    def test_invalid_input_returns_usage(self, settings):
        report = run_on_demand_scan("not a real ticker !!!", settings)
        assert "أرسل رمز زوج أو أصل" in report

    def test_no_data_available(self, monkeypatch, settings):
        monkeypatch.setattr("src.telegram_bot.scan_regime_with_data", lambda *a, **k: None)
        report = run_on_demand_scan("EURUSD", settings)
        assert "تعذّر جلب بيانات السوق" in report
        assert "EUR/USD" in report

    def test_found_signal_report(self, monkeypatch, settings):
        df = md.make_trend_pullback_bullish()
        metrics = _build_metrics(df)
        regime, confidence = classify_regime(metrics, settings)
        result = RegimeResult("EURUSD", "1h", regime, metrics, confidence)
        monkeypatch.setattr(
            "src.telegram_bot.scan_regime_with_data", lambda *a, **k: (result, df)
        )
        report = run_on_demand_scan("EURUSD", settings)
        assert "تقرير تحليل: EUR/USD" in report
        assert "التوصية: شراء" in report
        assert "ارتداد مع الاتجاه" in report
        assert "سعر الدخول:" in report and "وقف الخسارة:" in report

    def test_no_trade_report(self, monkeypatch, settings):
        df = md.make_low_liquidity_chop()
        metrics = _build_metrics(df)
        result = RegimeResult("EURUSD", "1h", MarketRegime.LOW_LIQUIDITY_CHOP, metrics, 0.6)
        monkeypatch.setattr(
            "src.telegram_bot.scan_regime_with_data", lambda *a, **k: (result, df)
        )
        report = run_on_demand_scan("EURUSD", settings)
        assert "لا تدخل صفقة حالياً" in report
        assert "لا توجد استراتيجية مناسبة الآن" in report
        assert "السيولة ضعيفة والسوق متذبذب" in report

    def test_scan_auto_adds_symbol_to_watchlist(self, monkeypatch, tmp_path, settings):
        df = md.make_low_liquidity_chop()
        metrics = _build_metrics(df)
        result = RegimeResult("GBPJPY", "1h", MarketRegime.LOW_LIQUIDITY_CHOP, metrics, 0.6)
        monkeypatch.setattr("src.telegram_bot.scan_regime_with_data", lambda *a, **k: (result, df))

        store = WatchlistStore(path=tmp_path / "watchlist.json", defaults=["EURUSD"])
        monkeypatch.setattr("src.telegram_bot.watchlist_store", store)

        assert "GBPJPY" not in store.get_all()
        run_on_demand_scan("gbp/jpy", settings)
        assert "GBPJPY" in store.get_all()

    def test_scan_does_not_add_on_fetch_failure(self, monkeypatch, tmp_path, settings):
        monkeypatch.setattr("src.telegram_bot.scan_regime_with_data", lambda *a, **k: None)
        store = WatchlistStore(path=tmp_path / "watchlist.json", defaults=["EURUSD"])
        monkeypatch.setattr("src.telegram_bot.watchlist_store", store)

        run_on_demand_scan("AUDNZD", settings)
        assert "AUDNZD" not in store.get_all()


# ---------------------------------------------------------------------------
# src/watchlist_store.py
# ---------------------------------------------------------------------------


class TestWatchlistStore:
    def test_defaults_seeded_when_file_absent(self, tmp_path):
        store = WatchlistStore(path=tmp_path / "watchlist.json")
        assert store.get_all() == sorted(DEFAULT_WATCHLIST)
        assert (tmp_path / "watchlist.json").exists()

    def test_add_new_symbol(self, tmp_path):
        store = WatchlistStore(path=tmp_path / "watchlist.json", defaults=["EURUSD"])
        symbol, was_new = store.add("gold")
        assert symbol == "XAUUSD"
        assert was_new is True
        assert "XAUUSD" in store.get_all()

    def test_add_existing_symbol_is_not_new(self, tmp_path):
        store = WatchlistStore(path=tmp_path / "watchlist.json", defaults=["EURUSD"])
        symbol, was_new = store.add("eurusd")
        assert symbol == "EURUSD"
        assert was_new is False

    def test_add_invalid_symbol_returns_none(self, tmp_path):
        store = WatchlistStore(path=tmp_path / "watchlist.json", defaults=["EURUSD"])
        symbol, was_new = store.add("???")
        assert symbol is None
        assert was_new is False

    def test_remove_existing_symbol(self, tmp_path):
        store = WatchlistStore(path=tmp_path / "watchlist.json", defaults=["EURUSD", "GBPUSD"])
        symbol, was_removed = store.remove("eurusd")
        assert symbol == "EURUSD"
        assert was_removed is True
        assert "EURUSD" not in store.get_all()

    def test_remove_nonexistent_symbol(self, tmp_path):
        store = WatchlistStore(path=tmp_path / "watchlist.json", defaults=["EURUSD"])
        symbol, was_removed = store.remove("USDJPY")
        assert symbol == "USDJPY"
        assert was_removed is False

    def test_persistence_across_instances(self, tmp_path):
        path = tmp_path / "watchlist.json"
        store1 = WatchlistStore(path=path, defaults=["EURUSD"])
        store1.add("gold")
        store2 = WatchlistStore(path=path, defaults=["EURUSD"])
        assert "XAUUSD" in store2.get_all()

    def test_contains(self, tmp_path):
        store = WatchlistStore(path=tmp_path / "watchlist.json", defaults=["EURUSD"])
        assert store.contains("EURUSD") is True
        assert store.contains("BTCUSD") is False


# ---------------------------------------------------------------------------
# scanner_1_regime.scan_symbols_with_data (dynamic-watchlist scanning path)
# ---------------------------------------------------------------------------


class TestScanSymbolsWithData:
    def test_resolves_metal_symbol_via_fetch_symbol_override(self, monkeypatch, settings):
        captured_fetch_symbols = []

        def fake_scan_regime_with_data(symbol, timeframe, settings, fetch_symbol=None):
            captured_fetch_symbols.append((symbol, fetch_symbol))
            df = md.make_ranging_consolidation()
            metrics = _build_metrics(df)
            regime, confidence = classify_regime(metrics, settings)
            return RegimeResult(symbol, timeframe, regime, metrics, confidence), df

        monkeypatch.setattr("src.scanner_1_regime.scan_regime_with_data", fake_scan_regime_with_data)
        results = scan_symbols_with_data(["XAUUSD", "EURUSD"], settings)

        assert len(results) == 2 * len(settings.timeframe_list)
        fetch_map = dict(captured_fetch_symbols)
        assert fetch_map["XAUUSD"] == "GC=F"
        assert fetch_map["EURUSD"] == "EURUSD=X"
        # canonical symbol identity is preserved on the result, not the fetch ticker
        assert all(r.symbol in {"XAUUSD", "EURUSD"} for r, _ in results)
