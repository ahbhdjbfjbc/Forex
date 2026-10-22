"""Regression tests for additional robustness/security findings from final audit."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import Settings
from src import notifier, oanda_client, strategy_params
from src.models import TradeDirection
from strategies.trend_pullback import TrendPullbackParams


def test_applescript_message_escaping_removes_statement_breaks() -> None:
    raw = 'hello "; do shell script "unsafe"\nnext line\\tail\x00'
    escaped = notifier._escape_applescript_string(raw)

    assert '\\"' in escaped
    assert "\n" not in escaped
    assert "\r" not in escaped
    assert "\x00" not in escaped
    assert "\\\\tail" in escaped


def test_send_alert_isolates_adapter_exceptions_and_does_not_log_exception_text(monkeypatch, caplog) -> None:
    monkeypatch.setattr(notifier, "format_message", lambda _signal: "test signal")

    def broken(_message, _settings):
        raise RuntimeError("credential-url-secret-must-not-be-logged")

    monkeypatch.setattr(notifier, "send_telegram", broken)
    monkeypatch.setattr(notifier, "send_discord", lambda _message, _settings: True)
    monkeypatch.setattr(notifier, "send_desktop", lambda _message, _settings: False)

    results = notifier.send_alert(object(), Settings(_env_file=None))

    assert results == {"telegram": False, "discord": True, "desktop": False}
    assert "credential-url-secret-must-not-be-logged" not in caplog.text


def test_strategy_params_invalid_root_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "strategy_params.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")

    params = strategy_params.get_strategy_params("trend_pullback", path=path)

    assert params == TrendPullbackParams()


def test_strategy_params_invalid_field_type_falls_back_to_field_default(tmp_path: Path) -> None:
    path = tmp_path / "strategy_params.json"
    path.write_text(json.dumps({"trend_pullback": {"rsi_bullish_ceiling": "not-a-number"}}), encoding="utf-8")

    params = strategy_params.get_strategy_params("trend_pullback", path=path)

    assert params.rsi_bullish_ceiling == TrendPullbackParams().rsi_bullish_ceiling


def test_strategy_params_save_is_valid_json_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "data" / "strategy_params.json"
    path.parent.mkdir(parents=True)
    path.write_text('{ broken json', encoding="utf-8")

    strategy_params.save_strategy_params("trend_pullback", TrendPullbackParams(), path=path)

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["trend_pullback"]["rsi_bullish_ceiling"] == TrendPullbackParams().rsi_bullish_ceiling
    assert not list(path.parent.glob("*.tmp"))
    assert not list(path.parent.glob(".*.tmp"))


def test_list_proposals_skips_corrupt_json_files(monkeypatch, tmp_path: Path) -> None:
    import strategy_evolver

    proposals = tmp_path / "proposals"
    proposals.mkdir()
    (proposals / "badc0ffe.json").write_text('{ broken', encoding="utf-8")
    (proposals / "not-a-proposal.json").write_text('{"created_at": "x"}', encoding="utf-8")
    monkeypatch.setattr(strategy_evolver, "PROPOSALS_DIR", proposals)

    assert strategy_evolver.list_proposals() == []


def test_oanda_market_order_rejects_invalid_direction_before_network(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        paper_trading_env="practice",
        paper_account_id="demo-account",
        paper_api_token="not-a-real-token",
    )
    monkeypatch.setattr(
        oanda_client.httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("network request must not be made"),
    )

    with pytest.raises(oanda_client.PaperTradingConfigError, match="direction"):
        oanda_client.place_market_order("EURUSD", "BUY", 1000, 1.09, 1.11, settings)  # type: ignore[arg-type]


def test_oanda_market_order_rejects_wrong_side_stop_and_target(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        paper_trading_env="practice",
        paper_account_id="demo-account",
        paper_api_token="not-a-real-token",
    )
    monkeypatch.setattr(
        oanda_client.httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("network request must not be made"),
    )

    with pytest.raises(oanda_client.PaperTradingConfigError, match="stop-loss"):
        oanda_client.place_market_order("EURUSD", TradeDirection.BUY, 1000, 1.12, 1.11, settings)


def test_oanda_order_error_does_not_return_response_body_or_credentials(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        paper_trading_env="practice",
        paper_account_id="demo-account",
        paper_api_token="not-a-real-token",
    )

    class FakeResponse:
        status_code = 401

        @staticmethod
        def json():
            return {"errorMessage": "echoed-not-a-real-token"}

    monkeypatch.setattr(oanda_client.httpx, "post", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(oanda_client.PaperTradingRequestError) as exc_info:
        oanda_client.place_market_order("EURUSD", TradeDirection.BUY, 1000, 1.09, 1.11, settings)

    assert "echoed-not-a-real-token" not in str(exc_info.value)
    assert "not-a-real-token" not in str(exc_info.value)


def test_oanda_account_summary_rejects_non_json_response_without_echoing_body(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        paper_trading_env="practice",
        paper_account_id="demo-account",
        paper_api_token="not-a-real-token",
    )

    class FakeResponse:
        status_code = 200
        text = "not-json response with fake-secret"

        @staticmethod
        def json():
            raise ValueError("not JSON")

    monkeypatch.setattr(oanda_client.httpx, "get", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(oanda_client.PaperTradingRequestError) as exc_info:
        oanda_client.get_account_summary(settings)

    assert "fake-secret" not in str(exc_info.value)
    assert "not-json" not in str(exc_info.value)


def test_oanda_order_rejects_malformed_success_payload(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        paper_trading_env="practice",
        paper_account_id="demo-account",
        paper_api_token="not-a-real-token",
    )

    class FakeResponse:
        status_code = 201

        @staticmethod
        def json():
            return {"unexpected": "payload"}

    monkeypatch.setattr(oanda_client.httpx, "post", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(oanda_client.PaperTradingRequestError, match="neither a fill nor a cancellation"):
        oanda_client.place_market_order("EURUSD", TradeDirection.BUY, 1000, 1.09, 1.11, settings)
