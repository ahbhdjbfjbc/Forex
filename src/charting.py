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
from src.price_format import format_price
from src.signal_context import build_signal_context
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
    first_visible = len(numeric) - len(visible)
    # Same context object the Telegram caption uses -> identical numbers.
    context = build_signal_context(signal, numeric)
    support = float(context.support)
    resistance = float(context.resistance)

    def price_label(value: float) -> str:
        return format_price(value, signal.symbol)
    ema20 = visible["Close"].ewm(span=20, adjust=False).mean()
    target = float(signal.take_profit_2_price)

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(10, 7.5), dpi=160)
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

    # Analysis zones: FVG / order blocks that support the trade, from where
    # they formed to the latest bar (computed only from closed candles).
    last_x = len(visible) - 1
    structure = context.structure
    structure_offset = structure.offset if structure is not None else 0
    for zone in context.zones:
        start = max(0, structure_offset + zone.start_index - first_visible)
        color = "#8f6bff" if zone.kind == "FVG" else "#3fb6c8"
        label = "FVG" if zone.kind == "FVG" else "ORDER BLOCK"
        ax.add_patch(Rectangle((start - 0.5, zone.low), last_x - start + 1.0, zone.high - zone.low,
                               facecolor=color, alpha=0.18, edgecolor=color, linewidth=0.8, zorder=1))
        ax.text(start, zone.high, f" {label}", color=color, fontsize=10, fontweight="bold",
                va="bottom", ha="left", zorder=4)
    if context.entry_high > context.entry_low:
        ax.axhspan(context.entry_low, context.entry_high, color="#f5ce5b", alpha=0.16, zorder=1,
                   label="Entry zone")
    if structure is not None and structure.last_event is not None:
        event = structure.last_event
        event_x = structure_offset + event.bar_index - first_visible
        if 0 <= event_x <= last_x:
            color = "#42d392" if event.direction == "BULLISH" else "#ff5a66"
            ax.hlines(event.level, max(0, event_x - 8), event_x, color=color, linewidth=1.4, zorder=4)
            ax.text(event_x, event.level, f" {event.kind}", color=color, fontsize=10,
                    fontweight="bold", va="bottom" if event.direction == "BULLISH" else "top", zorder=5)
    levels = [
        (float(signal.entry_price), "ENTRY", "#f5ce5b", "-"),
        (float(signal.stop_loss_price), "STOP", "#ff5a66", "--"),
        (target, "TARGET", "#42d392", "--"),
        (support, "SUPPORT", "#b28cff", ":"),
        (resistance, "RESISTANCE", "#ff9d66", ":"),
    ]
    for y, label, color, style in levels:
        ax.axhline(y, color=color, linestyle=style, linewidth=1.25, alpha=0.9)
    # Keep right-hand labels from overlapping when levels are close together.
    ax.autoscale_view()
    low_lim, high_lim = ax.get_ylim()
    low_lim = min(low_lim, min(y for y, *_ in levels))
    high_lim = max(high_lim, max(y for y, *_ in levels))
    ax.set_ylim(low_lim, high_lim)
    min_gap = (high_lim - low_lim) * 0.04
    placed: list[float] = []
    for y, label, color, _style in sorted(levels, key=lambda item: item[0]):
        label_y = y if not placed or y - placed[-1] >= min_gap else placed[-1] + min_gap
        placed.append(label_y)
        ax.text(1.01, label_y, f"{label}  {price_label(y)}", transform=ax.get_yaxis_transform(),
                color=color, fontsize=11, fontweight="bold", va="center", ha="left", clip_on=False)

    time_labels = []
    for value in visible.index:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("UTC")
        time_labels.append(stamp.strftime("%m-%d %H:%M"))
    tick_step = max(1, len(x_values) // 8)
    ticks = x_values[::tick_step]
    ax.set_xticks(ticks)
    ax.set_xticklabels([time_labels[i] for i in ticks], rotation=25, ha="right", fontsize=10)
    ax.tick_params(axis="y", labelsize=10)
    ax.grid(color="#718096", linewidth=0.4, alpha=0.18)
    ax.set_ylabel("Price")
    direction = "BUY" if signal.direction == TradeDirection.BUY else "SELL"
    source_label = "Yahoo Finance OHLCV"
    if signal.symbol.upper() == "XAUUSD":
        source_label += " · GC=F COMEX gold-futures proxy (not spot)"
    direction_color = "#21c78a" if direction == "BUY" else "#f05d6c"
    fig.text(0.10, 0.965, "TONPO Signals", color="#f5ce5b", fontsize=17, fontweight="bold", va="top")
    fig.text(0.78, 0.965, f"{signal.symbol}  ·  {signal.timeframe.upper()}  ·  {direction}",
             color=direction_color, fontsize=17, fontweight="bold", va="top", ha="right")
    fig.text(0.10, 0.915,
             f"Entry {price_label(signal.entry_price)}  ·  SL {price_label(signal.stop_loss_price)}  ·  Target {price_label(target)}",
             color="#c8d2e2", fontsize=12, va="top")
    created = pd.Timestamp(signal.timestamp)
    if created.tzinfo is not None:
        created = created.tz_convert("UTC")
    fig.text(0.12, 0.035,
             f"Created {created.strftime('%Y-%m-%d %H:%M:%S')} UTC  |  {source_label}  |  OHLCV candles are the analysis source",
             color="#9ba9bf", fontsize=8.5)
    ax.legend(loc="upper left", facecolor="#111a2e", edgecolor="#34415a", fontsize=10)
    fig.subplots_adjust(left=0.10, right=0.78, top=0.86, bottom=0.16)
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
            "entry_zone": [float(context.entry_low), float(context.entry_high)],
            "analysis_zones": [
                {"kind": z.kind, "direction": z.direction, "low": z.low, "high": z.high} for z in context.zones
            ],
            "last_structure_break": (
                {"kind": context.structure.last_event.kind, "direction": context.structure.last_event.direction,
                 "level": context.structure.last_event.level}
                if context.structure is not None and context.structure.last_event is not None else None
            ),
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
