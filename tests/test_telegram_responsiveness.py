"""End-to-end: drive the real python-telegram-bot polling loop against a fake
Bot API and prove that a slow admin command no longer blocks other commands."""
import asyncio
import time

import pytest

from config import Settings
from tests.fake_telegram import FakeTelegram, msg_update


def test_start_answers_while_slow_scan_is_running(monkeypatch, tmp_path):
    import telegram.ext as ext
    from src import telegram_bot as bot

    settings = Settings(_env_file=None, telegram_bot_token="123:abc", telegram_allowed_user_id="111")
    monkeypatch.setattr(bot, "get_settings", lambda: settings)
    monkeypatch.setattr(bot, "run_full_watchlist_scan", lambda *_a, **_k: time.sleep(2.0) or "scan report")

    fake = FakeTelegram([
        msg_update(1, "/scan_all"),
        msg_update(2, "/start"),
        msg_update(3, "/id", user_id=555),
    ])
    original_build = ext.ApplicationBuilder.build

    def build(self):
        self.request(fake).get_updates_request(fake)
        return original_build(self)

    monkeypatch.setattr(ext.ApplicationBuilder, "build", build)

    async def run():
        app = bot.build_telegram_application(settings)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(poll_interval=0.05)
        await asyncio.sleep(2.8)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

    asyncio.run(run())
    sent = {text.split("\n")[0][:20]: (at, chat) for at, chat, text in fake.sent}
    start_reply = next(at for at, chat, text in fake.sent if "متصل ويعمل" in text)
    scan_reply = next(at for at, chat, text in fake.sent if text == "scan report")
    id_reply = next((at, chat, text) for at, chat, text in fake.sent if "رقم حسابك" in text)
    assert start_reply < 1.0 < scan_reply, sent
    assert id_reply[1] == 555 and "555" in id_reply[2]


@pytest.mark.parametrize(
    ("scan_result", "expected_reply"),
    [
        (
            "🎯 التوصية: دخول صفقة BUY\n"
            "📍 سعر الدخول: 1.08500\n"
            "🛑 وقف الخسارة: 1.08000\n"
            "🎯 الهدف الوحيد: 1.09500",
            "اكتمل التحليل",
        ),
        (RuntimeError("market data unavailable"), "تعذر إكمال الفحص"),
    ],
)
def test_private_scan_is_safe_and_never_silent(monkeypatch, scan_result, expected_reply):
    import telegram.ext as ext
    from src import telegram_bot as bot

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_allowed_user_id="111",
    )
    monkeypatch.setattr(bot, "get_settings", lambda: settings)
    def run_scan(*_args, **_kwargs):
        if isinstance(scan_result, Exception):
            raise scan_result
        return scan_result

    monkeypatch.setattr(bot, "run_on_demand_scan", run_scan)
    fake = FakeTelegram([msg_update(10, "/scan EURUSD")])
    original_build = ext.ApplicationBuilder.build

    def build(self):
        self.request(fake).get_updates_request(fake)
        return original_build(self)

    monkeypatch.setattr(ext.ApplicationBuilder, "build", build)

    async def run():
        app = bot.build_telegram_application(settings)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(poll_interval=0.05)
        await asyncio.sleep(0.8)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

    asyncio.run(run())
    replies = [text for _, chat, text in fake.sent if chat == 111]
    assert any(expected_reply in text for text in replies)
    assert not any("1.08500" in text or "1.08000" in text or "1.09500" in text for text in replies)
