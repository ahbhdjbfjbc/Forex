"""Local-only and optional remote review tests; external HTTP is mocked."""
from config import Settings
from src import ai_assistant


def test_local_review_uses_closed_outcomes_and_discloses_small_samples(tmp_path, monkeypatch):
    rows = [
        {"symbol": "EURUSD", "timeframe": "1h", "strategy": "trend_pullback",
         "outcome": "TAKE_PROFIT_2", "r_multiple": 3.0, "quality_score": 85},
        {"symbol": "EURUSD", "timeframe": "1h", "strategy": "trend_pullback",
         "outcome": "STOP_LOSS", "r_multiple": -1.0, "quality_score": 75},
    ]
    monkeypatch.setattr(ai_assistant.signal_lifecycle, "closed_history", lambda limit=500: rows)
    monkeypatch.setattr(ai_assistant, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(ai_assistant.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no remote call")))

    report = ai_assistant.generate_review(Settings(_env_file=None))
    assert "2" in report
    assert "1 ربح / 1 غير رابحة" in report
    assert "العينة صغيرة" in report
    assert list((tmp_path / "ai_reviews").glob("*.json"))


def test_remote_review_requires_https_explicit_credentials_and_keeps_key_out_of_report(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ai_assistant.signal_lifecycle, "closed_history",
        lambda limit=500: [{"symbol": "XAUUSD", "timeframe": "1h", "strategy": "trend_pullback",
                            "outcome": "STOP_LOSS", "r_multiple": -1.0, "quality_score": 72}],
    )
    monkeypatch.setattr(ai_assistant, "get_data_dir", lambda: tmp_path)
    calls = []

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "هذه ملاحظة تجريبية"}}]}

    monkeypatch.setattr(ai_assistant.httpx, "post", lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    settings = Settings(
        _env_file=None, ai_review_enabled=True,
        ai_api_base_url="https://api.example.test/v1", ai_api_key="SECRET-DO-NOT-STORE", ai_model="test-model",
    )
    report = ai_assistant.generate_review(settings)
    assert "هذه ملاحظة تجريبية" in report
    assert len(calls) == 1
    assert calls[0][0] == "https://api.example.test/v1/chat/completions"
    saved = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "ai_reviews").glob("*.json"))
    assert "SECRET-DO-NOT-STORE" not in saved


def test_periodic_report_sends_privately_once_per_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_assistant, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(ai_assistant.signal_lifecycle, "closed_history", lambda limit=500: [])
    sent = []
    from src import notifier
    monkeypatch.setattr(notifier, "send_admin_message", lambda message, settings: sent.append(message) or True)
    settings = Settings(
        _env_file=None, ai_review_enabled=True,
        telegram_allowed_user_id="123456", telegram_bot_token="dummy",
    )

    assert ai_assistant.maybe_send_periodic_report(settings) is True
    assert ai_assistant.maybe_send_periodic_report(settings) is False
    assert len(sent) == 1
    assert "لا توجد نتائج محسومة" in sent[0]


def test_periodic_report_fails_closed_without_numeric_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_assistant, "get_data_dir", lambda: tmp_path)
    settings = Settings(_env_file=None, ai_review_enabled=True, telegram_bot_token="dummy")
    assert ai_assistant.maybe_send_periodic_report(settings) is False
