"""Tests for reproducible signal charts and atomic Telegram photo delivery."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from config import Settings
from src import charting, notifier
from src.models import (
    MarketRegime, RiskParameters, StrategyType, TradeDirection, TradeSignal,
)


def _frame(bars=70):
    close = np.linspace(2300.0, 2310.0, bars)
    return pd.DataFrame(
        {
            "Open": close - 0.4,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(bars, 1000.0),
        },
        index=pd.date_range("2026-10-20", periods=bars, freq="1h", tz="UTC"),
    )


def _signal():
    return TradeSignal(
        symbol="XAUUSD", timeframe="1h", direction=TradeDirection.BUY,
        strategy=StrategyType.TREND_PULLBACK, regime=MarketRegime.STRONG_TREND_BULLISH,
        entry_price=2310.0, stop_loss_price=2305.0, stop_loss_pips=5.0,
        take_profit_1_price=2320.0, take_profit_1_pips=10.0, take_profit_1_rr=2.0,
        take_profit_2_price=2325.0, take_profit_2_pips=15.0, take_profit_2_rr=3.0,
        risk=RiskParameters(10000, 1, 5, 0, 20, 100), rationale="test",
        timestamp=datetime.now(timezone.utc), quality_score=90, quality_grade="A",
    )


def test_chart_assets_are_built_from_saved_source_frame_and_match_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(charting, "get_data_dir", lambda: tmp_path)
    frame = _frame()
    signal = _signal()
    assets = charting.create_signal_chart(signal, frame, "signal-123")

    assert assets.image_path.is_file()
    assert assets.image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    source = json.loads(assets.source_path.read_text(encoding="utf-8"))
    assert source["signal_id"] == "signal-123"
    assert source["source"].startswith("Yahoo Finance OHLCV")
    assert "not spot" in source["source"]
    assert source["levels"]["entry"] == signal.entry_price
    assert source["levels"]["stop_loss"] == signal.stop_loss_price
    assert source["levels"]["single_chart_target"] == signal.take_profit_2_price
    assert len(source["ohlcv"]) == len(frame)
    assert source["ohlcv"][-1]["Close"] == frame["Close"].iloc[-1]


def test_chart_rejects_inconsistent_ohlc(tmp_path, monkeypatch):
    monkeypatch.setattr(charting, "get_data_dir", lambda: tmp_path)
    frame = _frame()
    frame.iloc[-1, frame.columns.get_loc("High")] = 1.0
    with pytest.raises(ValueError, match="OHLC high is inconsistent"):
        charting.create_signal_chart(_signal(), frame, "bad")


def test_telegram_photo_uses_single_sendphoto_and_checks_api_ack(tmp_path, monkeypatch):
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    settings = Settings(
        _env_file=None, telegram_bot_token="token", telegram_chat_id="111", telegram_channel_id="-100123"
    )
    sent = {}

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"ok": True}

    def fake_post(url, **kwargs):
        sent["url"] = url
        sent["data"] = kwargs["data"]
        sent["files"] = kwargs["files"]
        return Response()

    monkeypatch.setattr(notifier.httpx, "post", fake_post)
    assert notifier.send_telegram_photo("caption", image, settings) is True
    assert sent["url"].endswith("/sendPhoto")
    assert sent["data"] == {"chat_id": "-100123", "caption": "caption"}
    assert "photo" in sent["files"]

    monkeypatch.setattr(Response, "json", staticmethod(lambda: {"ok": False}))
    assert notifier.send_telegram_photo("caption", image, settings) is False


def test_text_formatter_has_one_target_and_gold_source_disclosure():
    message = notifier.format_message(_signal(), _frame())
    assert message.count("الهدف الوحيد") == 1
    assert "الهدف الأول" not in message and "الهدف الثاني" not in message
    assert "GC=F" in message and "ليس سعراً فورياً" in message
    assert "دعم/مقاومة" in message
