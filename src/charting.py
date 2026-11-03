"""Create reproducible mobile-readable signal charts from the exact analyzed OHLCV frame."""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Rectangle

from src.models import TradeDirection, TradeSignal
from src.storage import get_data_dir
from src.symbol_resolver import instrument_type

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChartAssets:
    image_path: Path
    source_path: Path


def _price_text(value: float) -> str:
    return format(float(value), ".10g")


def create_signal_chart(signal: TradeSignal, frame: pd.DataFrame, signal_id: str) -> ChartAssets:
    """Write a PNG plus a JSON record of every source candle and plotted level."""
    required = {"Open", "High", "Low", "Close", "Volume"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise ValueError("a complete OHLCV frame is required to publish a chart")
    numeric = frame[list(required)].apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any() or not all(math.isfinite(float(v)) for v in signal_prices(signal)):
        raise ValueError("OHLCV data and signal levels must be finite")
    if (numeric[["Open", "High", "Low", "Close"]] <= 0).any().any():
        raise ValueError("OHLC prices must be positive")
    if (numeric["High"] < numeric[["Open", "Low", "Close"]].max(axis=1)).any():
        raise ValueError("OHLC high is inconsistent")
    if (numeric["Low"] > numeric[["Open", "High", "Close"]].min(axis=1)).any():
        raise ValueError("OHLC low is inconsistent")

    chart_dir = get_data_dir() / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    image_path = chart_dir / f"{signal_id}.png"
    source_path = chart_dir / f"{signal_id}.json"
    visible = numeric.tail(min(100, len(numeric))).copy()
    previous = numeric.iloc[:-1].tail(50)
    support = float(previous["Low"].min()) if not previous.empty else float(visible["Low"].min())
    resistance = float(previous["High"].max()) if not previous.empty else float(visible["High"].max())
    ema20 = visible["Close"].ewm(span=20, adjust=False).mean()
    target = float(signal.take_profit_2_price)

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(10, 7), dpi=180)
    fig.patch.set_facecolor("#0b1020")
    ax.set_facecolor("#111a2e")
    up_color, down_color = "#21c78a", "#f05d6c"
    width = 0.62
    x_values = list(range(len(visible)))
    for x, (_, row) in zip(x_values, visible.iterrows()):
        color = up_color if row["Close"] >= row["Open"] else down_color
        ax.vlines(x, row["Low"], row["High"], color=color, linewidth=1.0, zorder=2)
        body_bottom = min(row["Open"], row["Close"])
        body_height = max(abs(row["Close"] - row["Open"]), max(abs(row["Close"]) * 0.00001, 1e-8))
        ax.add_patch(Rectangle((x - width / 2, body_bottom), width, body_height,
                               facecolor=color, edgecolor=color, linewidth=0.6, zorder=3))

    ax.plot(x_values, ema20.to_list(), color="#62a8ff", linewidth=1.25, alpha=0.9, label="EMA 20")
    levels = [
        (float(signal.entry_price), "ENTRY", "#f5ce5b", "-"),
        (float(signal.stop_loss_price), "STOP", "#ff5a66", "--"),
        (target, "TARGET", "#42d392", "--"),
        (support, "SUPPORT", "#b28cff", ":"),
        (resistance, "RESISTANCE", "#ff9d66", ":"),
    ]
    for y, label, color, style in levels:
        ax.axhline(y, color=color, linestyle=style, linewidth=1.25, alpha=0.9)
        ax.text(1.005, y, f"{label}  {_price_text(y)}", transform=ax.get_yaxis_transform(),
                color=color, fontsize=8, va="center", ha="left", clip_on=False)

    time_labels = []
    for value in visible.index:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("UTC")
        time_labels.append(stamp.strftime("%m-%d %H:%M"))
    tick_step = max(1, len(x_values) // 8)
    ticks = x_values[::tick_step]
    ax.set_xticks(ticks)
    ax.set_xticklabels([time_labels[i] for i in ticks], rotation=25, ha="right", fontsize=8)
    ax.grid(color="#718096", linewidth=0.4, alpha=0.18)
    ax.set_ylabel("Price")
    direction = "BUY" if signal.direction == TradeDirection.BUY else "SELL"
    source_label = "Yahoo Finance OHLCV"
    if signal.symbol.upper() == "XAUUSD":
        source_label += " · GC=F COMEX gold-futures proxy (not spot)"
    title = f"TONPO Signals  |  {signal.symbol}  |  {signal.timeframe.upper()}  |  {direction}"
    ax.set_title(title, loc="left", fontsize=14, color="#f5f7fb", pad=20, fontweight="bold")
    fig.text(0.12, 0.925,
             f"Entry {_price_text(signal.entry_price)}  ·  SL {_price_text(signal.stop_loss_price)}  ·  Target {_price_text(target)}",
             color="#c8d2e2", fontsize=9)
    created = pd.Timestamp(signal.timestamp)
    if created.tzinfo is not None:
        created = created.tz_convert("UTC")
    fig.text(0.12, 0.035,
             f"Created {created.strftime('%Y-%m-%d %H:%M:%S')} UTC  |  {source_label}  |  OHLCV candles are the analysis source",
             color="#9ba9bf", fontsize=7.5)
    ax.legend(loc="upper left", facecolor="#111a2e", edgecolor="#34415a", fontsize=8)
    fig.subplots_adjust(left=0.10, right=0.80, top=0.86, bottom=0.16)
    temporary_image = image_path.with_suffix(".png.tmp")
    fig.savefig(temporary_image, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    temporary_image.replace(image_path)

    source_record = {
        "signal_id": signal_id,
        "symbol": signal.symbol,
        "timeframe": signal.timeframe,
        "direction": direction,
        "created_at_utc": created.isoformat(),
        "source": source_label,
        "signal_context": {
            "strategy": str(getattr(signal.strategy, "value", signal.strategy)),
            "regime": str(getattr(signal.regime, "value", signal.regime)),
            "rationale": str(signal.rationale),
            "quality_score": signal.quality_score,
            "quality_grade": signal.quality_grade,
            "risk_percent": float(signal.risk.risk_percent),
            "recommended_lot_size": float(signal.risk.lot_size),
        },
        "levels": {
            "entry": float(signal.entry_price),
            "stop_loss": float(signal.stop_loss_price),
            "single_chart_target": target,
            "support": support,
            "resistance": resistance,
            "ema20_last": float(ema20.iloc[-1]),
        },
        "ohlcv": [
            {"timestamp": pd.Timestamp(index).isoformat(), **{key: float(value) for key, value in row.items()}}
            for index, row in numeric.iterrows()
        ],
    }
    temp_source = source_path.with_suffix(".json.tmp")
    temp_source.write_text(json.dumps(source_record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temp_source.replace(source_path)
    logger.info("created chart and source data for signal %s", signal_id)
    return ChartAssets(image_path=image_path, source_path=source_path)


def signal_prices(signal: TradeSignal) -> tuple[float, ...]:
    return (
        float(signal.entry_price), float(signal.stop_loss_price),
        float(signal.take_profit_1_price), float(signal.take_profit_2_price),
    )
