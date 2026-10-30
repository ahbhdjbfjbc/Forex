"""Regression tests for security and configuration hardening."""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from config import Settings
from src import notifier
from src.watchlist_store import WatchlistStore
from src.telegram_bot import _is_authorized


@pytest.mark.parametrize(("field", "value"), [
    ("account_size_usd", 0),
    ("account_size_usd", float("inf")),
    ("risk_percent_per_trade", -1),
    ("risk_percent_per_trade", 101),
    ("min_risk_reward", 0),
    ("scan_interval_seconds", 0),
    ("auto_scan_interval_seconds", -1),
    ("min_signal_confidence", 1.1),
    ("webhook_port", 65536),
])
def test_invalid_runtime_settings_are_rejected(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_watchlist_malformed_json_shape_falls_back_to_defaults(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(["EURUSD", "GBPUSD"]))
    store = WatchlistStore(path=path, defaults=["EURUSD"])
    assert store.get_all() == ["EURUSD"]


def test_watchlist_normalizes_symbols_from_disk(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"symbols": ["eur/usd", "gold", 42, "???"]}))
    store = WatchlistStore(path=path, defaults=["GBPUSD"])
    assert store.get_all() == ["EURUSD", "XAUUSD"]
    assert json.loads(path.read_text())["symbols"] == ["EURUSD", "XAUUSD"]


def test_telegram_http_error_does_not_log_secret_url(monkeypatch, caplog):
    token = "123456:TOP_SECRET_TEST_TOKEN"
    settings = Settings(_env_file=None, telegram_bot_token=token, telegram_chat_id="123")

    def raise_http_error(*args, **kwargs):
        raise httpx.RequestError(f"request failed for https://api.telegram.org/bot{token}/sendMessage")

    monkeypatch.setattr(notifier.httpx, "post", raise_http_error)
    with caplog.at_level(logging.ERROR):
        assert notifier.send_telegram("hello", settings) is False
    assert token not in caplog.text
    assert "RequestError" in caplog.text


def test_proposal_id_cannot_traverse_outside_proposal_directory(monkeypatch, tmp_path):
    import strategy_evolver as evolver

    proposals = tmp_path / "proposals"
    proposals.mkdir()
    marker = tmp_path / "outside.json"
    marker.write_text(json.dumps({"proposal_id": "deadbeef", "passed": True}))
    monkeypatch.setattr(evolver, "PROPOSALS_DIR", proposals)
    assert evolver.load_proposal("../outside") is None
    assert evolver.load_proposal("../../outside") is None
    assert evolver.load_proposal("deadbeef") is None


def test_group_chat_does_not_authorize_every_member_without_user_allowlist():
    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": -100123, "type": "supergroup"})(),
        "effective_user": type("UserStub", (), {"id": 456})(),
    })()
    settings = Settings(_env_file=None, telegram_chat_id="-100123")
    assert _is_authorized(update, settings) is False


def test_group_chat_requires_and_enforces_explicit_user_allowlist():
    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": -100123, "type": "supergroup"})(),
        "effective_user": type("UserStub", (), {"id": 456})(),
    })()
    allowed = Settings(_env_file=None, telegram_chat_id="-100123", telegram_allowed_user_id="456")
    denied = Settings(_env_file=None, telegram_chat_id="-100123", telegram_allowed_user_id="789")
    assert _is_authorized(update, allowed) is True
    assert _is_authorized(update, denied) is False


def test_basic_group_chat_also_requires_user_allowlist():
    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": -12345, "type": "group"})(),
        "effective_user": type("UserStub", (), {"id": 456})(),
    })()
    settings = Settings(_env_file=None, telegram_chat_id="-12345")
    assert _is_authorized(update, settings) is False


def test_private_control_panel_works_when_alerts_target_a_basic_group():
    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": 456, "type": "private"})(),
        "effective_user": type("UserStub", (), {"id": 456})(),
    })()
    settings = Settings(_env_file=None, telegram_chat_id="-12345")
    assert _is_authorized(update, settings) is True


def test_allowed_user_cannot_use_an_unconfigured_group():
    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": -999, "type": "group"})(),
        "effective_user": type("UserStub", (), {"id": 456})(),
    })()
    settings = Settings(
        _env_file=None,
        telegram_chat_id="-12345",
        telegram_allowed_user_id="456",
    )
    assert _is_authorized(update, settings) is False


def test_unauthorized_start_explains_why_the_panel_is_not_available(monkeypatch):
    import asyncio
    import src.telegram_bot as bot

    replies = []

    class MessageStub:
        async def reply_text(self, text, **kwargs):
            replies.append(text)

    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": 456, "type": "private"})(),
        "effective_user": type("UserStub", (), {"id": 456})(),
        "message": MessageStub(),
    })()
    settings = Settings(_env_file=None, telegram_allowed_user_id="789")

    monkeypatch.setattr(bot, "get_settings", lambda: settings)
    asyncio.run(bot.cmd_start(update, None))

    assert len(replies) == 1
    assert "لا تملك صلاحية" in replies[0]


def test_authorized_start_opens_the_telegram_control_keyboard(monkeypatch):
    import asyncio
    import src.telegram_bot as bot

    sent = []

    class MessageStub:
        async def reply_text(self, text, **kwargs):
            sent.append((text, kwargs))

    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": 456, "type": "private"})(),
        "effective_user": type("UserStub", (), {"id": 456})(),
        "message": MessageStub(),
    })()
    settings = Settings(_env_file=None, telegram_allowed_user_id="456")
    monkeypatch.setattr(bot, "get_settings", lambda: settings)

    asyncio.run(bot.cmd_start(update, None))

    assert len(sent) == 1
    assert "متصل ويعمل بنجاح" in sent[0][0]
    keyboard = sent[0][1]["reply_markup"]
    assert keyboard is not None
    assert len(keyboard.keyboard) == 4
    assert any(button.text == bot.BTN_QUICK_SCAN for row in keyboard.keyboard for button in row)


@pytest.mark.parametrize(
    ("telegram_delivered", "expected_text"),
    [
        (True, "تم إرسال توصية"),
        (False, "تعذر إرسال التوصية"),
    ],
)
def test_publish_callback_reports_actual_telegram_delivery(
    monkeypatch, telegram_delivered, expected_text
):
    import asyncio
    import src.telegram_bot as bot

    replies = []

    class MessageStub:
        async def reply_text(self, text, **kwargs):
            replies.append(text)

    class QueryStub:
        data = "pub:EURUSD"
        message = MessageStub()

        async def answer(self, *args, **kwargs):
            pass

    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": 456, "type": "private"})(),
        "effective_user": type("UserStub", (), {"id": 456})(),
        "callback_query": QueryStub(),
    })()
    settings = Settings(_env_file=None, telegram_allowed_user_id="456")
    monkeypatch.setattr(bot, "get_settings", lambda: settings)
    monkeypatch.setattr(bot, "scan_regime_with_data", lambda *args, **kwargs: (object(), object()))
    monkeypatch.setattr(bot, "generate_signal", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        bot,
        "send_alert",
        lambda *args, **kwargs: {"telegram": telegram_delivered, "discord": not telegram_delivered},
    )

    asyncio.run(bot.handle_callback_query(update, None))

    assert len(replies) == 1
    assert expected_text in replies[0]


def test_private_chat_still_works_without_extra_user_setting():
    update = type("UpdateStub", (), {
        "effective_chat": type("ChatStub", (), {"id": 123, "type": "private"})(),
        "effective_user": type("UserStub", (), {"id": 123})(),
    })()
    settings = Settings(_env_file=None, telegram_chat_id="123")
    assert _is_authorized(update, settings) is True


def test_discord_http_error_does_not_log_webhook_credential(monkeypatch, caplog):
    webhook = "https://discord.invalid/api/webhooks/123/TEST_WEBHOOK"
    settings = Settings(_env_file=None, discord_webhook_url=webhook)

    def raise_http_error(*args, **kwargs):
        raise httpx.RequestError(f"request failed for {webhook}")

    monkeypatch.setattr(notifier.httpx, "post", raise_http_error)
    with caplog.at_level(logging.ERROR):
        assert notifier.send_discord("hello", settings) is False
    assert webhook not in caplog.text
    assert "RequestError" in caplog.text


def test_no_signal_notification_uses_configured_scan_interval(monkeypatch):
    message = []
    settings = Settings(_env_file=None, auto_scan_interval_seconds=1800)
    monkeypatch.setattr(
        notifier,
        "dispatch_message",
        lambda text, _settings: message.append(text) or {"telegram": True},
    )

    notifier.send_no_signal_alert(3, settings)

    assert len(message) == 1
    assert "كل 30 دقيقة" in message[0]
    assert "بعد ساعة واحدة" not in message[0]
