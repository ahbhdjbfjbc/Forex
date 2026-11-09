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
    settings = Settings(
        _env_file=None,
        telegram_bot_token="1:x",
        telegram_chat_id="111",
        telegram_channel_id="-100",
    )
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
    assert kwargs["json"]["chat_id"] == "-100"
    assert kwargs["json"]["reply_parameters"] == {"message_id": 777, "allow_sending_without_reply": True}


def test_full_signal_chart_levels_and_final_outcome_use_the_configured_group(monkeypatch, tmp_path):
    from src import charting
    from src.publish_gate import GateDecision
    from tests.test_charting import _frame, _signal

    store = SignalLifecycleStore(tmp_path / "full-flow.sqlite3")
    monkeypatch.setattr(notifier, "signal_lifecycle", store)
    monkeypatch.setattr(charting, "get_data_dir", lambda: tmp_path / "charts")
    monkeypatch.setattr("src.publish_gate.check_publish_allowed", lambda *_a, **_k: GateDecision(True))
    settings = Settings(
        _env_file=None,
        telegram_bot_token="1:x",
        telegram_chat_id="111",
        telegram_allowed_user_id="111",
        telegram_channel_id="-100signals",
    )
    posts = []

    def fake_post(url, **kwargs):
        posts.append((url.rsplit("/", 1)[-1], kwargs))
        payload = {"ok": True, "result": {"message_id": 777}} if url.endswith("/sendPhoto") else {"ok": True}
        return _Resp(payload)

    monkeypatch.setattr(httpx, "post", fake_post)
    result = notifier.send_alert(_signal(), settings, source="integration-test", entry_bar_id="b0", market_frame=_frame())

    assert result["telegram"] is True
    assert posts[0][0] == "sendPhoto"
    assert posts[0][1]["data"]["chat_id"] == "-100signals"
    caption = posts[0][1]["data"]["caption"]
    assert "سعر الدخول:" in caption and "2310" in caption
    assert "وقف الخسارة:" in caption and "2305" in caption
    assert "الهدف الوحيد:" in caption and "2325" in caption

    outcome = store.observe_bar("XAUUSD", "1h", high=2326.0, low=2315.0, close=2325.0, bar_id="b1")
    assert outcome is not None and outcome.outcome == "TAKE_PROFIT_2"
    notifier.send_signal_outcome(outcome, settings)
    name, kwargs = posts[-1]
    assert name == "sendMessage"
    assert kwargs["json"]["chat_id"] == "-100signals"
    assert kwargs["json"]["reply_parameters"]["message_id"] == 777


def test_signal_photo_never_falls_back_to_private_chat(monkeypatch, tmp_path):
    settings = Settings(
        _env_file=None,
        telegram_bot_token="1:x",
        telegram_chat_id="111",
        telegram_allowed_user_id="111",
        telegram_channel_id="",
    )
    image = tmp_path / "c.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("must not send a signal to a private chat")
    ))

    assert notifier.send_telegram_photo("signal caption", image, settings) is False


def test_outcome_stays_pending_if_channel_reply_fails(monkeypatch, tmp_path):
    store = SignalLifecycleStore(tmp_path / "outbox.sqlite3")
    monkeypatch.setattr(notifier, "signal_lifecycle", store)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="1:x",
        telegram_allowed_user_id="111",
        telegram_channel_id="-100channel",
    )
    registration = store.register_signal(make_signal(), entry_bar_id="b0")
    store.set_telegram_message_id(registration.signal_id, 777)
    outcome = store.observe_bar(
        "EURUSD", "1h", high=1.1310, low=1.1150, close=1.1300, bar_id="b1"
    )
    assert outcome is not None
    marked = []
    monkeypatch.setattr(store, "mark_outcome_sent", lambda signal_id: marked.append(signal_id))
    monkeypatch.setattr(
        notifier,
        "dispatch_message",
        lambda *_args, **_kwargs: {"telegram": False, "discord": True, "desktop": False},
    )

    result = notifier.send_signal_outcome(outcome, settings)

    assert result["telegram"] is False
    assert result["discord"] is True
    assert marked == []


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
