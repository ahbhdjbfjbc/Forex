"""Tests for the strategy evolution & backtesting module: strategies/
package params, src/ast_patcher.py, backtester.py, strategy_evolver.py, and
src/oanda_client.py. All network calls (data fetch, OANDA HTTP) are mocked
— nothing here touches the network or the real data/ directory.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from backtester import BacktestResult, ExitReason, Trade, _compute_result, _resolve_eval_start, run_backtest
from config import Settings
from src import ast_patcher
from src import oanda_client as oc
from src.models import TradeDirection
from src.strategy_params import get_strategy_params, load_all_params, save_strategy_params
from strategies.breakout import BreakoutParams
from strategies.mean_reversion import MeanReversionParams
from strategies.trend_pullback import TrendPullbackParams
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
    )


# ---------------------------------------------------------------------------
# strategies/ package + src/strategy_params.py
# ---------------------------------------------------------------------------


class TestStrategyParams:
    def test_defaults_seeded_when_file_absent(self, tmp_path):
        path = tmp_path / "strategy_params.json"
        all_params = load_all_params(path)
        assert path.exists()
        assert set(all_params) == {"trend_pullback", "mean_reversion", "breakout"}

    def test_get_strategy_params_returns_dataclass_with_defaults(self, tmp_path):
        path = tmp_path / "strategy_params.json"
        params = get_strategy_params("trend_pullback", path=path)
        assert isinstance(params, TrendPullbackParams)
        assert params.rsi_bullish_ceiling == 55.0

    def test_overrides_layer_on_top_of_stored_values(self, tmp_path):
        path = tmp_path / "strategy_params.json"
        params = get_strategy_params("trend_pullback", overrides={"rsi_bullish_ceiling": 60.0}, path=path)
        assert params.rsi_bullish_ceiling == 60.0
        assert params.atr_sl_multiplier == 1.5  # untouched fields keep their stored value

    def test_unknown_override_keys_are_ignored_not_fatal(self, tmp_path):
        path = tmp_path / "strategy_params.json"
        params = get_strategy_params("trend_pullback", overrides={"not_a_real_field": 123}, path=path)
        assert isinstance(params, TrendPullbackParams)

    def test_save_then_reload_round_trips(self, tmp_path):
        path = tmp_path / "strategy_params.json"
        new_params = MeanReversionParams(rsi_overbought=80.0)
        save_strategy_params("mean_reversion", new_params, path=path)
        reloaded = get_strategy_params("mean_reversion", path=path)
        assert reloaded.rsi_overbought == 80.0

    def test_unknown_strategy_name_raises(self, tmp_path):
        path = tmp_path / "strategy_params.json"
        with pytest.raises(ValueError):
            get_strategy_params("not_a_strategy", path=path)


# ---------------------------------------------------------------------------
# src/ast_patcher.py
# ---------------------------------------------------------------------------


class TestAstPatcher:
    @pytest.fixture
    def sample_file(self, tmp_path) -> Path:
        target = tmp_path / "mean_reversion_copy.py"
        shutil.copy(Path(__file__).resolve().parent.parent / "strategies" / "mean_reversion.py", target)
        return target

    def test_read_constant_class_attribute(self, sample_file):
        assert ast_patcher.read_constant(sample_file, "rsi_overbought", class_name="MeanReversionParams") == 70.0

    def test_patch_constant_changes_only_target_line(self, sample_file):
        original_text = sample_file.read_text()
        ast_patcher.patch_constant(sample_file, "rsi_overbought", 80.0, class_name="MeanReversionParams")

        patched_text = sample_file.read_text()
        assert ast_patcher.read_constant(sample_file, "rsi_overbought", class_name="MeanReversionParams") == 80.0

        original_lines = original_text.splitlines()
        patched_lines = patched_text.splitlines()
        changed = [i for i, (a, b) in enumerate(zip(original_lines, patched_lines)) if a != b]
        assert changed == [next(i for i, l in enumerate(original_lines) if "rsi_overbought" in l)]
        assert len(original_lines) == len(patched_lines)  # no lines added/removed

    def test_dry_run_does_not_write(self, sample_file):
        original_text = sample_file.read_text()
        ast_patcher.patch_constant(sample_file, "rsi_overbought", 99.0, class_name="MeanReversionParams", dry_run=True)
        assert sample_file.read_text() == original_text

    def test_missing_target_raises(self, sample_file):
        with pytest.raises(ast_patcher.PatchTargetNotFoundError):
            ast_patcher.patch_constant(sample_file, "does_not_exist", 1)

    def test_missing_class_raises(self, sample_file):
        with pytest.raises(ast_patcher.PatchTargetNotFoundError):
            ast_patcher.patch_constant(sample_file, "rsi_overbought", 1.0, class_name="NoSuchClass")

    def test_patched_file_still_parses(self, sample_file):
        import ast as ast_module

        patched_source = ast_patcher.patch_constant(
            sample_file, "rsi_overbought", 12.5, class_name="MeanReversionParams", dry_run=True
        )
        ast_module.parse(patched_source)  # raises SyntaxError if broken


# ---------------------------------------------------------------------------
# backtester.py — pure metrics math (hand-verified expected values)
# ---------------------------------------------------------------------------


class TestBacktesterMetrics:
    def _make_trade(self, r_multiple: float, entry_time, exit_time) -> Trade:
        return Trade(
            symbol="EURUSD", timeframe="1h", direction=TradeDirection.BUY,
            entry_time=entry_time, entry_price=1.1000,
            exit_time=exit_time, exit_price=1.1000 + r_multiple * 0.0010,
            stop_loss_price=1.0990, take_profit_1_price=1.1020,
            exit_reason=ExitReason.TAKE_PROFIT_1 if r_multiple > 0 else ExitReason.STOP_LOSS,
            r_multiple=r_multiple,
        )

    def test_win_rate_and_profit_factor_hand_verified(self):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        trades = [
            self._make_trade(2.0, base, base + timedelta(hours=1)),
            self._make_trade(-1.0, base + timedelta(days=1), base + timedelta(days=1, hours=1)),
            self._make_trade(2.0, base + timedelta(days=2), base + timedelta(days=2, hours=1)),
            self._make_trade(-1.0, base + timedelta(days=3), base + timedelta(days=3, hours=1)),
        ]
        equity_curve = {}
        equity = 1.0
        for t in trades:
            equity *= 1 + t.r_multiple * 0.01  # 1% risk per trade
            equity_curve[t.exit_time] = equity

        df = pd.DataFrame(index=pd.date_range(base, periods=5, freq="1D"))
        result = _compute_result("EURUSD", "1h", df, 0, trades, equity_curve)

        assert result.total_trades == 4
        assert result.win_rate_pct == 50.0
        assert result.profit_factor == pytest.approx(2.0)  # gross_profit=4.0, gross_loss=2.0
        assert result.avg_realized_r == pytest.approx(0.5)

    def test_no_trades_returns_zeroed_result(self):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        df = pd.DataFrame(index=pd.date_range(base, periods=5, freq="1D"))
        result = _compute_result("EURUSD", "1h", df, 0, [], {base: 1.0})
        assert result.total_trades == 0
        assert result.win_rate_pct == 0.0
        assert result.profit_factor == 0.0
        assert result.passes_acceptance_criteria() is False

    def test_all_winners_gives_infinite_profit_factor(self):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        trades = [self._make_trade(2.0, base, base + timedelta(hours=1))]
        df = pd.DataFrame(index=pd.date_range(base, periods=2, freq="1D"))
        result = _compute_result("EURUSD", "1h", df, 0, trades, {base: 1.02})
        assert result.profit_factor == float("inf")

    def test_max_drawdown_computed_from_equity_curve(self):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # Equity rises to 1.10, falls to 0.99 (a ~10% drawdown from peak), then recovers.
        equity_curve = {
            base: 1.00,
            base + timedelta(days=1): 1.10,
            base + timedelta(days=2): 0.99,
            base + timedelta(days=3): 1.05,
        }
        trades = [self._make_trade(1.0, base, base + timedelta(days=1))]
        df = pd.DataFrame(index=pd.date_range(base, periods=4, freq="1D"))
        result = _compute_result("EURUSD", "1h", df, 0, trades, equity_curve)
        expected_dd = (1.10 - 0.99) / 1.10 * 100
        assert result.max_drawdown_pct == pytest.approx(expected_dd, abs=0.01)

    def test_passes_acceptance_criteria_boundary(self):
        result = BacktestResult(
            symbol="EURUSD", timeframe="1h", start=datetime.now(timezone.utc), end=datetime.now(timezone.utc),
            bars_tested=100, trades=[], equity_curve=pd.Series(dtype=float),
            total_trades=10, win_rate_pct=50.0, profit_factor=1.31, sharpe_ratio=1.51, max_drawdown_pct=11.9,
        )
        assert result.passes_acceptance_criteria(1.5, 1.3, 12.0) is True
        assert result.passes_acceptance_criteria(1.6, 1.3, 12.0) is False  # sharpe just under a stricter bar


class TestResolveEvalStart:
    def test_none_lookback_uses_min_warmup(self):
        df = pd.DataFrame(index=pd.date_range("2024-01-01", periods=1000, freq="1h"))
        from backtester import MIN_WARMUP_BARS

        assert _resolve_eval_start(df, None) == MIN_WARMUP_BARS

    def test_lookback_months_clamped_to_min_warmup(self):
        # A huge lookback (bigger than the whole dataset) should still clamp
        # to MIN_WARMUP_BARS rather than go negative or before bar 0.
        df = pd.DataFrame(index=pd.date_range("2024-01-01", periods=300, freq="1h"))
        from backtester import MIN_WARMUP_BARS

        assert _resolve_eval_start(df, lookback_months=36) == MIN_WARMUP_BARS


# ---------------------------------------------------------------------------
# backtester.py — full walk-forward integration (data fetch mocked)
# ---------------------------------------------------------------------------


class TestRunBacktestIntegration:
    def test_runs_end_to_end_on_synthetic_trending_data(self, monkeypatch, settings):
        # A long bullish trend with repeated pullbacks gives the walk-forward
        # loop real entry/exit activity to chew on, without hitting the network.
        df = md.make_strong_trend_bullish(bars=400, seed=7)
        monkeypatch.setattr("backtester.fetch_ohlcv", lambda request: df)

        result = run_backtest("EURUSD", "1h", settings, lookback_months=None)
        assert result is not None
        assert result.bars_tested > 0
        assert isinstance(result.passes_acceptance_criteria(), bool)

    def test_insufficient_data_returns_none(self, monkeypatch, settings):
        short_df = md.make_strong_trend_bullish(bars=50, seed=7)
        monkeypatch.setattr("backtester.fetch_ohlcv", lambda request: short_df)
        assert run_backtest("EURUSD", "1h", settings) is None

    def test_no_data_returns_none(self, monkeypatch, settings):
        monkeypatch.setattr("backtester.fetch_ohlcv", lambda request: None)
        assert run_backtest("EURUSD", "1h", settings) is None

    def test_param_overrides_change_backtest_outcome(self, monkeypatch, settings):
        df = md.make_strong_trend_bullish(bars=400, seed=7)
        monkeypatch.setattr("backtester.fetch_ohlcv", lambda request: df)

        baseline = run_backtest("EURUSD", "1h", settings, lookback_months=None)
        # An absurdly tight EMA touch tolerance should suppress virtually
        # every trend-pullback entry, so the trade count should drop.
        overridden = run_backtest(
            "EURUSD", "1h", settings, lookback_months=None,
            param_overrides={"trend_pullback": {"ema_touch_tolerance_pct": 0.0}},
        )
        assert overridden.total_trades <= baseline.total_trades


# ---------------------------------------------------------------------------
# strategy_evolver.py
# ---------------------------------------------------------------------------


class TestStrategyEvolver:
    def test_grid_cartesian_product(self):
        import strategy_evolver as se

        combos = se._grid({"a": [1, 2], "b": [10, 20]})
        assert len(combos) == 4
        assert {"a": 1, "b": 10} in combos
        assert {"a": 2, "b": 20} in combos

    def test_empty_grid_returns_single_empty_override(self):
        import strategy_evolver as se

        assert se._grid({}) == [{}]

    def test_evolve_strategy_saves_proposals(self, monkeypatch, tmp_path, settings):
        import strategy_evolver as se

        monkeypatch.setattr(se, "PROPOSALS_DIR", tmp_path / "proposals")

        df = md.make_strong_trend_bullish(bars=400, seed=7)
        monkeypatch.setattr("backtester.fetch_ohlcv", lambda request: df)

        candidates = se.evolve_strategy(
            "trend_pullback", "EURUSD", "1h", {"rsi_bullish_ceiling": [50.0, 60.0]}, settings, lookback_months=None
        )
        assert len(candidates) == 2
        assert (tmp_path / "proposals").exists()
        saved_files = list((tmp_path / "proposals").glob("*.json"))
        assert len(saved_files) == 2

    def test_unknown_strategy_raises(self, settings):
        import strategy_evolver as se

        with pytest.raises(ValueError):
            se.evolve_strategy("not_a_strategy", "EURUSD", "1h", {}, settings)

    def test_apply_proposal_refuses_failing_candidate(self, monkeypatch, tmp_path):
        import strategy_evolver as se

        monkeypatch.setattr(se, "PROPOSALS_DIR", tmp_path / "proposals")
        monkeypatch.setattr(se, "BACKUPS_DIR", tmp_path / "backups")
        params_path = tmp_path / "strategy_params.json"

        proposal_id = "deadbeef"
        (tmp_path / "proposals").mkdir(parents=True)
        (tmp_path / "proposals" / f"{proposal_id}.json").write_text(json.dumps({
            "proposal_id": proposal_id, "strategy_name": "trend_pullback",
            "param_overrides": {"rsi_bullish_ceiling": 99.0}, "passed": False,
            "metrics": {}, "created_at": "2024-01-01T00:00:00+00:00",
            "symbol": "EURUSD", "timeframe": "1h",
        }))

        assert se.apply_proposal(proposal_id, params_path=params_path) is False
        assert not params_path.exists()  # never touched

    def test_apply_proposal_writes_params_and_backs_up(self, monkeypatch, tmp_path):
        import strategy_evolver as se

        proposals_dir = tmp_path / "proposals"
        backups_dir = tmp_path / "backups"
        params_path = tmp_path / "strategy_params.json"
        monkeypatch.setattr(se, "PROPOSALS_DIR", proposals_dir)
        monkeypatch.setattr(se, "BACKUPS_DIR", backups_dir)

        save_strategy_params("trend_pullback", TrendPullbackParams(), path=params_path)  # seed baseline

        proposal_id = "cafef00d"
        proposals_dir.mkdir(parents=True)
        (proposals_dir / f"{proposal_id}.json").write_text(json.dumps({
            "proposal_id": proposal_id, "strategy_name": "trend_pullback",
            "param_overrides": {"rsi_bullish_ceiling": 62.0}, "passed": True,
            "metrics": {}, "created_at": "2024-01-01T00:00:00+00:00",
            "symbol": "EURUSD", "timeframe": "1h",
        }))

        assert se.apply_proposal(proposal_id, params_path=params_path) is True
        updated = get_strategy_params("trend_pullback", path=params_path)
        assert updated.rsi_bullish_ceiling == 62.0
        assert list(backups_dir.glob("*.json"))  # a backup was written

    def test_apply_nonexistent_proposal_returns_false(self, monkeypatch, tmp_path):
        import strategy_evolver as se

        monkeypatch.setattr(se, "PROPOSALS_DIR", tmp_path / "proposals")
        assert se.apply_proposal("no-such-id") is False


# ---------------------------------------------------------------------------
# src/oanda_client.py — HTTP mocked
# ---------------------------------------------------------------------------


class TestOandaClient:
    def test_refuses_non_practice_env(self):
        settings = Settings(_env_file=None, paper_trading_env="live", paper_account_id="x", paper_api_token="y")
        with pytest.raises(oc.PaperTradingConfigError):
            oc._base_url(settings)

    def test_accepts_practice_env(self):
        settings = Settings(_env_file=None, paper_trading_env="practice", paper_account_id="x", paper_api_token="y")
        assert oc._base_url(settings) == oc._PRACTICE_BASE_URL

    def test_missing_token_raises(self):
        settings = Settings(_env_file=None, paper_trading_env="practice", paper_account_id="x", paper_api_token="")
        with pytest.raises(oc.PaperTradingConfigError):
            oc._headers(settings)

    def test_missing_account_id_raises(self):
        settings = Settings(_env_file=None, paper_trading_env="practice", paper_account_id="", paper_api_token="y")
        with pytest.raises(oc.PaperTradingConfigError):
            oc._require_account_id(settings)

    @pytest.mark.parametrize("symbol,expected", [("EURUSD", "EUR_USD"), ("XAUUSD", "XAU_USD")])
    def test_to_oanda_instrument_fx_and_metals(self, symbol, expected):
        assert oc.to_oanda_instrument(symbol) == expected

    def test_to_oanda_instrument_rejects_crypto(self):
        with pytest.raises(oc.PaperTradingConfigError):
            oc.to_oanda_instrument("BTCUSD")

    def test_place_market_order_success(self, monkeypatch):
        settings = Settings(_env_file=None, paper_trading_env="practice", paper_account_id="acc1", paper_api_token="tok")

        class FakeResponse:
            status_code = 201

            def json(self):
                return {
                    "orderCreateTransaction": {"id": "1"},
                    "orderFillTransaction": {"price": "1.10500", "tradeOpened": {"tradeID": "42"}},
                }

        monkeypatch.setattr(oc.httpx, "post", lambda *a, **k: FakeResponse())

        result = oc.place_market_order("EURUSD", TradeDirection.BUY, 10_000, 1.1000, 1.1100, settings)
        assert result.status == "FILLED"
        assert result.trade_id == "42"
        assert result.fill_price == pytest.approx(1.105)

    def test_place_market_order_rejected(self, monkeypatch):
        settings = Settings(_env_file=None, paper_trading_env="practice", paper_account_id="acc1", paper_api_token="tok")

        class FakeResponse:
            status_code = 400

            def json(self):
                return {"errorMessage": "INSUFFICIENT_MARGIN"}

        monkeypatch.setattr(oc.httpx, "post", lambda *a, **k: FakeResponse())

        with pytest.raises(oc.PaperTradingRequestError):
            oc.place_market_order("EURUSD", TradeDirection.BUY, 10_000, 1.1000, 1.1100, settings)

    def test_get_account_summary_success(self, monkeypatch):
        settings = Settings(_env_file=None, paper_trading_env="practice", paper_account_id="acc1", paper_api_token="tok")

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"account": {"balance": "100000", "currency": "USD"}}

        monkeypatch.setattr(oc.httpx, "get", lambda *a, **k: FakeResponse())
        summary = oc.get_account_summary(settings)
        assert summary["account"]["balance"] == "100000"

    def test_get_account_summary_http_error_raises(self, monkeypatch):
        settings = Settings(_env_file=None, paper_trading_env="practice", paper_account_id="acc1", paper_api_token="tok")

        class FakeResponse:
            status_code = 401
            text = "unauthorized"

        monkeypatch.setattr(oc.httpx, "get", lambda *a, **k: FakeResponse())
        with pytest.raises(oc.PaperTradingRequestError):
            oc.get_account_summary(settings)


# ---------------------------------------------------------------------------
# src/paper_executor.py
# ---------------------------------------------------------------------------


class TestPaperExecutor:
    def test_noop_when_disabled(self, settings):
        from src.paper_executor import execute_paper_trade

        settings2 = Settings(_env_file=None, enable_paper_trading=False)
        assert execute_paper_trade(None, settings2) is False

    def test_skips_non_fx_instrument(self, monkeypatch):
        from src.models import RegimeResult, StrategyType, TradeSignal
        from src.paper_executor import execute_paper_trade

        settings = Settings(_env_file=None, enable_paper_trading=True)
        called = []
        monkeypatch.setattr("src.paper_executor.oanda_client.place_market_order", lambda *a, **k: called.append(1))

        signal = _make_fake_signal(symbol="BTCUSD")
        assert execute_paper_trade(signal, settings) is False
        assert not called


# ---------------------------------------------------------------------------
# src/telegram_bot.py — /evolve report formatting (backtest itself mocked)
# ---------------------------------------------------------------------------


class TestTelegramEvolveReport:
    def _fake_candidate(self, passed: bool, proposal_id: str = "abc123"):
        import strategy_evolver as se
        from backtester import BacktestResult

        result = BacktestResult(
            symbol="EURUSD", timeframe="1h",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 6, 30, tzinfo=timezone.utc),
            bars_tested=1000, trades=[], equity_curve=pd.Series(dtype=float),
            total_trades=142, win_rate_pct=64.2, profit_factor=1.82, sharpe_ratio=1.71, max_drawdown_pct=8.4,
            avg_realized_r=0.44,
        )
        return se.EvolutionCandidate(
            proposal_id=proposal_id, strategy_name="trend_pullback", symbol="EURUSD", timeframe="1h",
            param_overrides={"rsi_oversold": 25.0, "atr_sl_multiplier": 1.2},
            result=result, passed=passed, created_at=datetime.now(timezone.utc),
        )

    def test_passed_report_tells_user_to_apply(self, tmp_path):
        from src.telegram_bot import _format_evolution_report

        candidate = self._fake_candidate(passed=True)
        report = _format_evolution_report("trend_pullback", "EURUSD", "1h", candidate, tested_count=9)
        assert "اجتاز معايير القبول بنجاح" in report
        assert "/apply abc123" in report
        assert "نسبة النجاح (Win Rate): 64.2%" in report
        assert "نسبة شارب (Sharpe Ratio): 1.71" in report
        assert "تم التطبيق" not in report  # applying is a separate explicit command

    def test_failed_report_does_not_offer_apply(self, tmp_path):
        from src.telegram_bot import _format_evolution_report

        candidate = self._fake_candidate(passed=False)
        report = _format_evolution_report("trend_pullback", "EURUSD", "1h", candidate, tested_count=9)
        assert "لم يجتز معايير الأمان الصارمة" in report
        assert "/apply" not in report

    def test_run_evolution_no_candidates_reports_insufficient_data(self, monkeypatch, settings):
        import strategy_evolver as se
        from src.telegram_bot import run_evolution

        monkeypatch.setattr(se, "evolve_strategy", lambda *a, **k: [])
        report = run_evolution("trend_pullback", "EURUSD", "1h", settings)
        assert "لا توجد نتائج اختبار كافية" in report
        assert "البيانات التاريخية غير كافية" in report

    def test_run_evolution_uses_best_sharpe_candidate(self, monkeypatch, settings):
        import strategy_evolver as se
        from src.telegram_bot import run_evolution

        best = self._fake_candidate(passed=True, proposal_id="best1")
        worse = self._fake_candidate(passed=False, proposal_id="worse1")
        monkeypatch.setattr(se, "evolve_strategy", lambda *a, **k: [best, worse])  # already sorted best-first

        report = run_evolution("trend_pullback", "EURUSD", "1h", settings)
        assert "best1" in report
        assert "worse1" not in report


def _make_fake_signal(symbol: str):
    from datetime import datetime, timezone

    from src.models import MarketRegime, RiskParameters, StrategyType, TradeDirection, TradeSignal

    return TradeSignal(
        symbol=symbol, timeframe="1h", direction=TradeDirection.BUY,
        strategy=StrategyType.TREND_PULLBACK, regime=MarketRegime.STRONG_TREND_BULLISH,
        entry_price=1.1000, stop_loss_price=1.0950, stop_loss_pips=50.0,
        take_profit_1_price=1.1100, take_profit_1_pips=100.0, take_profit_1_rr=2.0,
        take_profit_2_price=1.1150, take_profit_2_pips=150.0, take_profit_2_rr=3.0,
        risk=RiskParameters(
            account_size_usd=10_000, risk_percent=1.0, stop_loss_pips=50.0,
            pip_value_per_lot=10.0, lot_size=0.2, risk_amount_usd=100.0,
        ),
        rationale="test", timestamp=datetime.now(timezone.utc),
    )
