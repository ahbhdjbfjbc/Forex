from config import Settings
from src import system_health


def test_health_notifications_are_transition_based_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(system_health, "get_data_dir", lambda: tmp_path)
    settings = Settings(_env_file=None, telegram_allowed_user_id="123", telegram_bot_token="token")
    sent = []
    sender = lambda message, _settings: sent.append(message) or True

    assert system_health.report_health("market_data", False, "source unavailable", settings, sender) is True
    assert system_health.report_health("market_data", False, "still unavailable", settings, sender) is False
    assert system_health.report_health("market_data", True, "source restored", settings, sender) is True
    assert len(sent) == 2
    assert "عطل" in sent[0]
    assert "تعافي" in sent[1]


def test_health_alert_retries_failed_delivery_and_healthy_start_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(system_health, "get_data_dir", lambda: tmp_path)
    settings = Settings(_env_file=None, telegram_allowed_user_id="123", telegram_bot_token="token")
    assert system_health.report_health("economic_calendar", True, "available", settings, lambda *_: True) is False
    attempts = []
    assert system_health.report_health("economic_calendar", False, "stale", settings,
                                       lambda *_: attempts.append("fail") or False) is False
    assert system_health.report_health("economic_calendar", False, "stale", settings,
                                       lambda *_: attempts.append("retry") or True) is True
    assert attempts == ["fail", "retry"]
