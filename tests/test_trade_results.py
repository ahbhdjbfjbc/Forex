"""Channel threading of outcomes and the /results, /trade views."""
import asyncio

import httpx

from config import Settings
from src import notifier
from src import telegram_bot as bot
from src.signal_lifecycle import SignalLifecycleStore
from tests.test_signal_lifecycle import make_signal


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_photo_message_id_is_stored_and_outcome_replies_to_it(monkeypatch, tmp_path):
    store = SignalLifecycleStore(tmp_path / "s.sqlite3")
    monkeypatch.setattr(notifier, "signal_lifecycle", store)
    settings = Settings(_env_file=None, telegram_bot_token="1:x", telegram_chat_id="-100")
    posts = []

    def fake_post(url, **kwargs):
        posts.append((url.rsplit("/", 1)[-1], kwargs))
        return _Resp({"ok": True, "result": {"message_id": 777}})

    monkeypatch.setattr(httpx, "post", fake_post)
    image = tmp_path / "c.png"
    image.write_bytes(b"png")
    registration = store.register_signal(make_signal(), entry_bar_id="b0")
    notifier._delivery_state.last_photo_message_id = None
    assert notifier.send_telegram_photo("caption", image, settings)
    assert notifier._delivery_state.last_photo_message_id == 777
    store.set_telegram_message_id(registration.signal_id, 777)

    outcome = store.observe_bar("EURUSD", "1h", high=1.1310, low=1.1150, close=1.1300, bar_id="b1")
    assert outcome is not None
    notifier.send_signal_outcome(outcome, settings)
    name, kwargs = posts[-1]
    assert name == "sendMessage"
    assert kwargs["json"]["reply_parameters"] == {"message_id": 777, "allow_sending_without_reply": True}


def test_results_page_and_trade_details(monkeypatch, tmp_path):
    store = SignalLifecycleStore(tmp_path / "s.sqlite3")
    monkeypatch.setattr(bot, "signal_lifecycle", store)
    win = store.register_signal(make_signal(), entry_bar_id="b0")
    store.register_signal(make_signal(symbol="AUDUSD=X"), entry_bar_id="b0")
    store.observe_bar("EURUSD", "1h", high=1.1310, low=1.1150, close=1.1300, bar_id="b1")

    text, markup = bot.build_results_page(0)
    assert "ربح 1" in text and "مفتوحة 1" in text
    assert f"#{win.signal_id[:8]}" in text
    assert "⏳ مفتوحة" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"trade:{win.signal_id[:16]}" in callbacks

    details = bot.format_trade_details(store.get_signal(win.signal_id))
    assert "سعر الخروج" in details and "+3R" in details and "المدة" in details

    sent = []

    class Target:
        async def reply_text(self, text, **_kw):
            sent.append(text)

    asyncio.run(bot._send_trade_details(Target(), win.signal_id[:8]))
    assert "تفاصيل الصفقة" in sent[0]
    asyncio.run(bot._send_trade_details(Target(), "ffffffff"))
    assert "لم أجد" in sent[1]
