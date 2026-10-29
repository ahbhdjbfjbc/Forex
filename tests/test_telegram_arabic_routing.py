"""Regression tests for Arabic Telegram alerts and destination routing."""
from __future__ import annotations

from types import SimpleNamespace

import httpx

from config import Settings
from src import notifier
from src.telegram_bot import _is_authorized


def _response(url: str, status_code: int = 200) -> httpx.Response:
    """Return a small HTTP response for mocked Telegram API calls."""
    return httpx.Response(status_code, request=httpx.Request("POST", url))


def test_notification_targets_override_command_chat_and_parse_commas() -> None:
    """Explicit notification destinations are parsed and trimmed."""
    settings = Settings(
        _env_file=None,
        telegram_chat_id="12345",
        telegram_notification_chat_ids=" -100100, , -100200 ",
    )

    assert settings.telegram_notification_targets == ["-100100", "-100200"]


def test_notification_target_defaults_to_existing_chat_for_compatibility() -> None:
    """The legacy command chat remains the destination when no list is set."""
    settings = Settings(_env_file=None, telegram_chat_id="12345")

    assert settings.telegram_notification_targets == ["12345"]


def test_outbound_group_does_not_authorize_group_commands() -> None:
    """An alert-only group does not acquire permission to issue bot commands."""
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100100, type="supergroup"),
        effective_user=SimpleNamespace(id=12345),
    )
    settings = Settings(
        _env_file=None,
        telegram_chat_id="12345",
        telegram_notification_chat_ids="-100100",
    )

    assert _is_authorized(update, settings) is False


def test_telegram_alert_is_sent_to_every_configured_target(monkeypatch) -> None:
    """A single alert is delivered to every configured group destination."""
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_chat_id="12345",
        telegram_notification_chat_ids="-100100,-100200",
    )
    delivered_to: list[str] = []

    def fake_post(url: str, *, json: dict, timeout: float) -> httpx.Response:
        assert timeout > 0
        delivered_to.append(json["chat_id"])
        return _response(url)

    monkeypatch.setattr(notifier.httpx, "post", fake_post)

    assert notifier.send_telegram("توصية تداول جديدة", settings) is True
    assert delivered_to == ["-100100", "-100200"]


def test_one_group_failure_does_not_prevent_delivery_to_other_groups(monkeypatch) -> None:
    """A failed group delivery does not block attempts to later destinations."""
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_notification_chat_ids="-100bad,-100good",
    )
    delivered_to: list[str] = []

    def fake_post(url: str, *, json: dict, timeout: float) -> httpx.Response:
        assert timeout > 0
        target = json["chat_id"]
        delivered_to.append(target)
        return _response(url, 400 if target == "-100bad" else 200)

    monkeypatch.setattr(notifier.httpx, "post", fake_post)

    assert notifier.send_telegram("اختبار", settings) is True
    assert delivered_to == ["-100bad", "-100good"]


def test_telegram_returns_false_when_all_configured_targets_fail(monkeypatch) -> None:
    """Telegram reports failure when every configured destination rejects delivery."""
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_notification_chat_ids="-100bad",
    )

    def reject_post(url: str, **_kwargs) -> httpx.Response:
        return _response(url, 403)

    monkeypatch.setattr(notifier.httpx, "post", reject_post)

    assert notifier.send_telegram("اختبار", settings) is False
