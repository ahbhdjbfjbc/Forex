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

    class FakeUpdater:
        async def start_polling(self, **kwargs):
            polling_calls.append(kwargs)

        async def stop(self):
            pass

    class FakeTelegramApplication:
        updater = FakeUpdater()

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


def test_qualified_live_signal_still_reaches_notifier(monkeypatch) -> None:
    import main as app

    signal = object()
    regime = SimpleNamespace(confidence=0.8, symbol="EURUSD", timeframe="1h")
    settings = Settings(_env_file=None, min_signal_confidence=0.6)
    alerts = []

    monkeypatch.setattr(app.watchlist_store, "get_all", lambda: ["EURUSD"])
    monkeypatch.setattr(app, "scan_symbols_with_data", lambda _symbols, _settings: [(regime, object())])
    monkeypatch.setattr(app, "generate_signal", lambda *_args, **_kwargs: signal)
    monkeypatch.setattr(app, "send_alert", lambda delivered, _settings: alerts.append(delivered) or {"telegram": True})
    monkeypatch.setattr(app, "execute_paper_trade", lambda *_args, **_kwargs: None)

    app.run_scan_cycle(settings)

    assert alerts == [signal]
