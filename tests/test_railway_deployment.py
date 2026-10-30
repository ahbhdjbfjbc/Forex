from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from config import Settings
from src import storage


def test_railway_port_overrides_local_webhook_port(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "34567")
    monkeypatch.setenv("WEBHOOK_PORT", "8123")
    settings = Settings(_env_file=None)
    assert settings.service_port == 34567


def test_local_webhook_port_is_used_when_railway_port_is_absent(monkeypatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("WEBHOOK_PORT", "8123")
    settings = Settings(_env_file=None)
    assert settings.service_port == 8123


def test_invalid_railway_port_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, port=0)


def test_configured_volume_path_is_used_for_runtime_data(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(forex_data_dir=tmp_path),
    )
    assert storage.get_data_dir() == tmp_path.resolve()


def test_first_scan_is_queued_in_background_before_server_start(monkeypatch) -> None:
    import main as app

    class FakeScheduler:
        def __init__(self) -> None:
            self.jobs = []
            self.started = False

        def add_job(self, func, trigger, **kwargs) -> None:
            self.jobs.append((func, trigger, kwargs))

        def start(self) -> None:
            self.started = True

        def shutdown(self, wait=False) -> None:
            pass

    settings = Settings(_env_file=None)
    scheduler = FakeScheduler()
    monkeypatch.setattr(app, "get_settings", lambda: settings)
    monkeypatch.setattr(app, "build_scheduler", lambda _settings: scheduler)
    monkeypatch.setattr(app, "run_scan_cycle", lambda *_a, **_k: pytest.fail("scan must not run inline"))
    monkeypatch.setattr(app.asyncio, "run", lambda coroutine: coroutine.close())

    app.main()

    assert scheduler.started
    assert any(
        func is app.run_scan_cycle and trigger == "date"
        for func, trigger, _kwargs in scheduler.jobs
    )


def test_telegram_polling_preserves_updates_received_during_restart(monkeypatch) -> None:
    import main as app

    polling_calls = []
    published_commands = []

    class FakeBot:
        async def set_my_commands(self, commands):
            published_commands.extend(command.command for command in commands)

    class FakeUpdater:
        async def start_polling(self, **kwargs):
            polling_calls.append(kwargs)

        async def stop(self):
            pass

    class FakeTelegramApplication:
        updater = FakeUpdater()
        bot = FakeBot()

        async def initialize(self):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

        async def shutdown(self):
            pass

    class FakeServer:
        def __init__(self, _config):
            pass

        async def serve(self):
            pass

    monkeypatch.setattr(app, "build_telegram_application", lambda _settings: FakeTelegramApplication())
    monkeypatch.setattr(app.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(app.uvicorn, "Server", FakeServer)

    asyncio.run(app._run_webhook_and_telegram(Settings(_env_file=None, telegram_bot_token="test-token")))

    assert polling_calls == [{
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }]
    assert "start" in published_commands
    assert "menu" in published_commands
    assert "scan" in published_commands


def test_telegram_command_menu_api_failure_does_not_disable_polling(monkeypatch) -> None:
    import main as app

    polling_calls = []

    class FakeBot:
        async def set_my_commands(self, _commands):
            raise RuntimeError("simulated Bot API failure")

    class FakeUpdater:
        async def start_polling(self, **kwargs):
            polling_calls.append(kwargs)

        async def stop(self):
            pass

    class FakeTelegramApplication:
        updater = FakeUpdater()
        bot = FakeBot()

        async def initialize(self):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

        async def shutdown(self):
            pass

    class FakeServer:
        def __init__(self, _config):
            pass

        async def serve(self):
            pass

    monkeypatch.setattr(app, "build_telegram_application", lambda _settings: FakeTelegramApplication())
    monkeypatch.setattr(app.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(app.uvicorn, "Server", FakeServer)

    asyncio.run(app._run_webhook_and_telegram(Settings(_env_file=None, telegram_bot_token="test-token")))

    assert len(polling_calls) == 1


def test_control_panel_commands_are_registered_with_telegram_application() -> None:
    from telegram.ext import CommandHandler
    from src.telegram_bot import build_telegram_application

    application = build_telegram_application(
        Settings(_env_file=None, telegram_bot_token="123:TEST")
    )
    registered = {
        command
        for handlers in application.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }

    assert {"start", "menu", "scan", "scan_all"} <= registered


def test_qualified_live_signal_still_reaches_notifier(monkeypatch) -> None:
    import main as app

    signal = SimpleNamespace(
        symbol="EURUSD", timeframe="1h", direction=SimpleNamespace(value="BUY"),
        strategy=SimpleNamespace(value="TREND_PULLBACK"), timestamp=None,
    )
    regime = SimpleNamespace(confidence=0.8, symbol="EURUSD", timeframe="1h")
    settings = Settings(_env_file=None, min_signal_confidence=0.6)
    frame = SimpleNamespace(index=["2026-10-30T16:00:00Z"])
    alerts = []
    paper_trades = []

    with app._auto_signal_lock:
        app._recent_auto_signal_keys.clear()

    monkeypatch.setattr(app.watchlist_store, "get_all", lambda: ["EURUSD"])
    monkeypatch.setattr(app, "monitor_news_state", lambda _settings: False)
    monkeypatch.setattr(app, "scan_symbols_with_data", lambda _symbols, _settings: [(regime, frame)])
    monkeypatch.setattr(app, "generate_signal", lambda *_args, **_kwargs: signal)
    monkeypatch.setattr(app, "send_alert", lambda delivered, _settings: alerts.append(delivered) or {"telegram": True})
    monkeypatch.setattr(app, "execute_paper_trade", lambda *args, **_kwargs: paper_trades.append(args))

    app.run_scan_cycle(settings)
    app.run_scan_cycle(settings)
    assert len(alerts) == 1
    assert len(paper_trades) == 1

    # A newly closed candle is a new opportunity and must still be dispatched.
    frame.index = ["2026-10-30T17:00:00Z"]
    app.run_scan_cycle(settings)
    assert alerts == [signal, signal]
    assert len(paper_trades) == 2


def test_no_signal_alerts_are_opt_in_to_avoid_repetitive_notifications(monkeypatch) -> None:
    import main as app

    settings = Settings(_env_file=None)
    assert settings.notify_no_signals is False
    regime_data = [(object(), object())]
    sent = []
    monkeypatch.setattr(app.watchlist_store, "get_all", lambda: ["EURUSD"])
    monkeypatch.setattr(app, "monitor_news_state", lambda _settings: False)
    monkeypatch.setattr(app, "scan_symbols_with_data", lambda *_args: regime_data)
    monkeypatch.setattr(app, "generate_signal", lambda *_args: None)
    monkeypatch.setattr(app, "send_no_signal_alert", lambda *_args: sent.append(True))

    app.run_scan_cycle(settings)
    assert sent == []

    app.run_scan_cycle(Settings(_env_file=None, notify_no_signals=True))
    assert sent == [True]
