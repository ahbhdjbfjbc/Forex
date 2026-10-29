from __future__ import annotations

import httpx

from config import Settings
from src import notifier
from src.telegram_bot import _is_authorized


def _response(url: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("POST", url))


def test_notification_targets_override_command_chat_and_parse_commas() -> None:
    settings = Settings(
        _env_file=None,
        telegram_chat_id="12345",
        telegram_notification_chat_ids=" -100100, , -100200 ",
    )

    assert settings.telegram_notification_targets == ["-100100", "-100200"]


def test_notification_target_defaults_to_existing_chat_for_compatibility() -> None:
    settings = Settings(_env_file=None, telegram_chat_id="12345")

    assert settings.telegram_notification_targets == ["12345"]


def test_outbound_group_does_not_authorize_group_commands() -> None:
    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": -100100, "type": "supergroup"})(),
        "effective_user": type("UserStub", (), {"id": 12345})(),
    })()
    settings = Settings(
        _env_file=None,
        telegram_chat_id="12345",
        telegram_notification_chat_ids="-100100",
    )

    assert _is_authorized(update, settings) is False


def test_telegram_alert_is_sent_to_every_configured_target(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_chat_id="12345",
        telegram_notification_chat_ids="-100100,-100200",
    )
    delivered_to: list[str] = []

    def fake_post(url: str, *, json: dict, timeout: float) -> httpx.Response:
        delivered_to.append(json["chat_id"])
        return _response(url)

    monkeypatch.setattr(notifier.httpx, "post", fake_post)

    assert notifier.send_telegram("توصية تداول جديدة", settings) is True
    assert delivered_to == ["-100100", "-100200"]


def test_one_group_failure_does_not_prevent_delivery_to_other_groups(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_notification_chat_ids="-100bad,-100good",
    )
    delivered_to: list[str] = []

    def fake_post(url: str, *, json: dict, timeout: float) -> httpx.Response:
        target = json["chat_id"]
        delivered_to.append(target)
        return _response(url, 400 if target == "-100bad" else 200)

    monkeypatch.setattr(notifier.httpx, "post", fake_post)

    assert notifier.send_telegram("اختبار", settings) is True
    assert delivered_to == ["-100bad", "-100good"]


def test_telegram_returns_false_when_all_configured_targets_fail(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_notification_chat_ids="-100bad",
    )

    monkeypatch.setattr(notifier.httpx, "post", lambda url, **kwargs: _response(url, 403))

    assert notifier.send_telegram("اختبار", settings) is False
