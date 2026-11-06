"""Proposes and backtests parameter variants for a strategy — a script, run
on demand, not an autonomous background loop.

Workflow:
    1. `evolve_strategy(...)` backtests every combination in a parameter
       grid, merged over the strategy's current baseline params, and saves
       each result as a proposal file under `data/strategy_proposals/`.
    2. You review the printed/returned results.
    3. `apply_proposal(proposal_id)` is the ONLY thing that writes to
       `data/strategy_params.json` — and only for a proposal that already
       passed the acceptance criteria (Sharpe > 1.5, Profit Factor > 1.3,
       Max Drawdown < 12%). Nothing here auto-applies a variant on its own;
       promoting a variant to the params that actually drive the live bot
       is always an explicit, separate step you take by proposal ID.

CLI:
    python strategy_evolver.py --strategy trend_pullback --symbol EURUSD \\
        --timeframe 1h --grid '{"rsi_bullish_ceiling": [50, 55, 60]}'
    python strategy_evolver.py --apply <proposal_id>
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import itertools
import json
import logging
import math
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import backtester
import pandas as pd
from config import Settings, get_settings
from backtester import BacktestResult, MIN_WARMUP_BARS, run_backtest
from src.strategy_params import DEFAULT_PARAMS_PATH, PARAM_CLASSES, get_strategy_params, save_strategy_params
from src.storage import get_data_dir
from src.symbol_resolver import to_yfinance_ticker

logger = logging.getLogger(__name__)

PROPOSALS_DIR = get_data_dir() / "strategy_proposals"
BACKUPS_DIR = get_data_dir() / "strategy_params_backups"

ACCEPTANCE_MIN_SHARPE = 1.5
ACCEPTANCE_MIN_PROFIT_FACTOR = 1.3
ACCEPTANCE_MAX_DRAWDOWN_PCT = 12.0
MIN_OUT_OF_SAMPLE_TRADES = 20
TRAIN_FRACTION = 0.70

# Sensible default search grids per strategy, used by the Telegram /evolve
# command (typing a raw JSON grid into a chat message is error-prone) and
# available to CLI/script callers too. Deliberately small (9-15 combos) so
# a chat-triggered run finishes in well under a minute; pass an explicit
# --grid on the CLI for a wider search.
DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "trend_pullback": {
        "rsi_bullish_ceiling": [45.0, 55.0, 65.0],
        "atr_sl_multiplier": [1.0, 1.5, 2.0],
    },
    "mean_reversion": {
        "rsi_oversold": [25.0, 30.0, 35.0],
        "rsi_overbought": [65.0, 70.0, 75.0],
    },
    "breakout": {
        "breakout_rvol_min": [1.0, 1.2, 1.5],
        "atr_sl_multiplier": [1.0, 1.5, 2.0],
    },
}

STRATEGY_DISPLAY_LABELS: dict[str, str] = {
    "trend_pullback": "Trend Pullback",
    "mean_reversion": "Mean Reversion",
    "breakout": "Breakout",
}


@dataclass(frozen=True)
class EvolutionCandidate:
    proposal_id: str
    strategy_name: str
    symbol: str
    timeframe: str
    param_overrides: dict[str, Any]
    result: BacktestResult
    out_of_sample_result: BacktestResult
    baseline_out_of_sample_result: BacktestResult
    validation: dict[str, Any]
    strategy_version: dict[str, str]
    passed: bool
    created_at: datetime


def _grid(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of {param: [values...]} -> list of override dicts.
    An empty grid means "just the current baseline, unmodified"."""
    if not param_grid:
        return [{}]
    keys = list(param_grid.keys())
    return [dict(zip(keys, combo)) for combo in itertools.product(*param_grid.values())]


def _strategy_version(strategy_name: str, params_path: Path | None = None) -> dict[str, str]:
    """Fingerprint the source file and baseline parameter set used by a proposal."""
    if strategy_name not in PARAM_CLASSES:
        raise ValueError(f"unknown strategy {strategy_name!r}")
    strategy_module = importlib.import_module(PARAM_CLASSES[strategy_name].__module__)
    source_path = Path(strategy_module.__file__)
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    params = asdict(get_strategy_params(strategy_name, path=params_path or DEFAULT_PARAMS_PATH))
    params_bytes = json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "source_sha256": source_hash,
        "baseline_params_sha256": hashlib.sha256(params_bytes).hexdigest(),
    }


def evolve_strategy(
    strategy_name: str,
    symbol: str,
    timeframe: str,
    param_grid: dict[str, list[Any]],
    settings: Settings | None = None,
    lookback_months: int | None = 6,
) -> list[EvolutionCandidate]:
    """Train on the earlier 70%, validate on a later 30%, and save proposals only.

    The OOS window is never used to rank candidates. Applying still requires a
    separate explicit admin action and a passing baseline comparison.
    """
    if strategy_name not in PARAM_CLASSES:
        raise ValueError(f"unknown strategy {strategy_name!r}; known: {sorted(PARAM_CLASSES)}")

    settings = settings or get_settings()
    candidates: list[EvolutionCandidate] = []
    strategy_version = _strategy_version(strategy_name)
    request = backtester.OHLCVRequest(
        symbol=to_yfinance_ticker(symbol), timeframe=timeframe
    )
    market_frame = backtester.fetch_ohlcv(request)
    if market_frame is None or market_frame.empty:
        logger.warning("no market data for chronological strategy evaluation")
        return []
    if lookback_months is not None:
        cutoff = market_frame.index[-1] - pd.DateOffset(months=lookback_months)
        start_index = max(0, int(market_frame.index.searchsorted(cutoff)) - MIN_WARMUP_BARS)
        market_frame = market_frame.iloc[start_index:]

    split = int(len(market_frame) * TRAIN_FRACTION)
    if split <= MIN_WARMUP_BARS or len(market_frame) - split < 30:
        logger.warning(
            "insufficient bars for chronological OOS evaluation (%d bars; need warm-up and holdout)",
            len(market_frame),
        )
        return []
    training_frame = market_frame.iloc[:split]
    # Keep only past bars needed for indicator warm-up; the first evaluated
    # bar of this slice is exactly the holdout boundary.
    test_frame = market_frame.iloc[split - MIN_WARMUP_BARS:]
    baseline_oos = run_backtest(
        symbol, timeframe, settings, lookback_months=None, ohlcv_frame=test_frame
    )
    if baseline_oos is None:
        logger.warning("baseline returned no result on the OOS period; refusing proposals")
        return []

    for overrides in _grid(param_grid):
        train_result = run_backtest(
            symbol, timeframe, settings,
            param_overrides={strategy_name: overrides},
            lookback_months=None, ohlcv_frame=training_frame,
        )
        result = run_backtest(
            symbol, timeframe, settings,
            param_overrides={strategy_name: overrides},
            lookback_months=None, ohlcv_frame=test_frame,
        )
        if train_result is None or result is None:
            logger.warning("skipping candidate %s: backtest returned no result", overrides)
            continue

        train_passed = train_result.passes_acceptance_criteria(
            ACCEPTANCE_MIN_SHARPE, ACCEPTANCE_MIN_PROFIT_FACTOR, ACCEPTANCE_MAX_DRAWDOWN_PCT
        )
        oos_passed = (
            result.total_trades >= MIN_OUT_OF_SAMPLE_TRADES
            and result.passes_acceptance_criteria(
                ACCEPTANCE_MIN_SHARPE, ACCEPTANCE_MIN_PROFIT_FACTOR, ACCEPTANCE_MAX_DRAWDOWN_PCT
            )
            and result.avg_realized_r > baseline_oos.avg_realized_r
        )
        validation = {
            "method": "chronological-70-30",
            "training_start": training_frame.index[0].isoformat(),
            "training_end": training_frame.index[-1].isoformat(),
            "out_of_sample_start": market_frame.index[split].isoformat(),
            "out_of_sample_end": market_frame.index[-1].isoformat(),
            "minimum_out_of_sample_trades": MIN_OUT_OF_SAMPLE_TRADES,
            "out_of_sample_trades": result.total_trades,
            "baseline_out_of_sample_trades": baseline_oos.total_trades,
            "candidate_out_of_sample_avg_r": result.avg_realized_r,
            "baseline_out_of_sample_avg_r": baseline_oos.avg_realized_r,
            "training_passed": train_passed,
            "out_of_sample_passed": oos_passed,
        }
        candidate = EvolutionCandidate(
            proposal_id=uuid4().hex[:8],
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            param_overrides=overrides,
            result=train_result,
            out_of_sample_result=result,
            baseline_out_of_sample_result=baseline_oos,
            validation=validation,
            strategy_version=strategy_version,
            passed=train_passed and oos_passed,
            created_at=datetime.now(timezone.utc),
        )
        candidates.append(candidate)
        _save_proposal(candidate)

    # Rank only by the training slice; never select a candidate based on its
    # holdout score.
    candidates.sort(key=lambda c: c.result.sharpe_ratio, reverse=True)
    return candidates


def _proposal_payload(candidate: EvolutionCandidate) -> dict[str, Any]:
    def metrics(result: BacktestResult) -> dict[str, Any]:
        return {
            "total_trades": result.total_trades,
            "win_rate_pct": result.win_rate_pct,
            "profit_factor": result.profit_factor,
            "sharpe_ratio": result.sharpe_ratio,
            "max_drawdown_pct": result.max_drawdown_pct,
            "avg_realized_r": result.avg_realized_r,
            "start": result.start.isoformat(),
            "end": result.end.isoformat(),
            "bars_tested": result.bars_tested,
        }

    r = candidate.result
    return {
        "proposal_id": candidate.proposal_id,
        "strategy_name": candidate.strategy_name,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "param_overrides": candidate.param_overrides,
        "passed": candidate.passed,
        "created_at": candidate.created_at.isoformat(),
        "metrics": {
            "training": metrics(r),
            "out_of_sample": metrics(candidate.out_of_sample_result),
            "baseline_out_of_sample": metrics(candidate.baseline_out_of_sample_result),
        },
        "validation": candidate.validation,
        "strategy_version": candidate.strategy_version,
    }


def _save_proposal(candidate: EvolutionCandidate) -> None:
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = PROPOSALS_DIR / f"{candidate.proposal_id}.json"
    path.write_text(json.dumps(_proposal_payload(candidate), indent=2))


def _valid_proposal_overrides(strategy_name: str, overrides: Any) -> bool:
    if strategy_name not in PARAM_CLASSES or not isinstance(overrides, dict):
        return False
    defaults = asdict(PARAM_CLASSES[strategy_name]())
    for name, value in overrides.items():
        if name not in defaults:
            return False
        default = defaults[name]
        if isinstance(default, bool):
            if type(value) is not bool:
                return False
        elif isinstance(default, int):
            if type(value) is not int:
                return False
        elif isinstance(default, float):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                return False
        elif not isinstance(value, type(default)):
            return False
    return True


def _valid_oos_validation(proposal: dict[str, Any]) -> bool:
    validation = proposal.get("validation")
    metrics = proposal.get("metrics")
    if not isinstance(validation, dict) or not isinstance(metrics, dict):
        return False
    oos = metrics.get("out_of_sample")
    baseline = metrics.get("baseline_out_of_sample")
    training = metrics.get("training")
    version = proposal.get("strategy_version")
    if (
        not isinstance(oos, dict) or not isinstance(baseline, dict)
        or not isinstance(training, dict) or not isinstance(version, dict)
    ):
        return False
    try:
        trade_count = int(validation["out_of_sample_trades"])
        candidate_avg = float(validation["candidate_out_of_sample_avg_r"])
        baseline_avg = float(validation["baseline_out_of_sample_avg_r"])
        train_sharpe = float(training["sharpe_ratio"])
        train_pf = float(training["profit_factor"])
        train_dd = float(training["max_drawdown_pct"])
        oos_sharpe = float(oos["sharpe_ratio"])
        oos_pf = float(oos["profit_factor"])
        oos_dd = float(oos["max_drawdown_pct"])
        split_valid = (
            datetime.fromisoformat(validation["training_end"])
            < datetime.fromisoformat(validation["out_of_sample_start"])
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    hashes_valid = all(
        isinstance(version.get(key), str)
        and len(version[key]) == 64
        and all(char in "0123456789abcdef" for char in version[key])
        for key in ("source_sha256", "baseline_params_sha256")
    )
    return (
        validation.get("method") == "chronological-70-30"
        and validation.get("training_passed") is True
        and validation.get("out_of_sample_passed") is True
        and trade_count >= MIN_OUT_OF_SAMPLE_TRADES
        and oos.get("total_trades") == trade_count
        and candidate_avg > baseline_avg
        and oos.get("avg_realized_r") == candidate_avg
        and baseline.get("avg_realized_r") == baseline_avg
        and train_sharpe > ACCEPTANCE_MIN_SHARPE
        and train_pf > ACCEPTANCE_MIN_PROFIT_FACTOR
        and train_dd < ACCEPTANCE_MAX_DRAWDOWN_PCT
        and oos_sharpe > ACCEPTANCE_MIN_SHARPE
        and oos_pf > ACCEPTANCE_MIN_PROFIT_FACTOR
        and oos_dd < ACCEPTANCE_MAX_DRAWDOWN_PCT
        and hashes_valid
        and split_valid
    )


def load_proposal(proposal_id: str) -> dict[str, Any] | None:
    # Generated proposals use uuid4().hex[:8]. Validate before constructing a
    # path so Telegram/CLI input cannot escape the proposal directory.
    if not isinstance(proposal_id, str) or re.fullmatch(r"[0-9a-f]{8}", proposal_id) is None:
        return None
    path = PROPOSALS_DIR / f"{proposal_id}.json"
    if not path.is_file():
        return None
    try:
        proposal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("could not load proposal %s (invalid or unreadable file)", proposal_id)
        return None
    if (
        not isinstance(proposal, dict)
        or proposal.get("proposal_id") != proposal_id
        or not isinstance(proposal.get("strategy_name"), str)
        or proposal.get("strategy_name") not in PARAM_CLASSES
        or not _valid_proposal_overrides(proposal.get("strategy_name"), proposal.get("param_overrides"))
        or type(proposal.get("passed")) is not bool
        or not isinstance(proposal.get("metrics"), dict)
        or not isinstance(proposal.get("created_at"), str)
    ):
        logger.warning("could not load proposal %s (invalid proposal structure)", proposal_id)
        return None
    if proposal.get("passed") is True and not _valid_oos_validation(proposal):
        logger.warning("refusing proposal %s without valid out-of-sample evidence", proposal_id)
        return None
    return proposal


def list_proposals() -> list[dict[str, Any]]:
    if not PROPOSALS_DIR.exists():
        return []
    proposals: list[dict[str, Any]] = []
    for path in sorted(PROPOSALS_DIR.glob("*.json")):
        proposal = load_proposal(path.stem)
        if proposal is not None:
            proposals.append(proposal)
    proposals.sort(key=lambda proposal: proposal.get("created_at", ""), reverse=True)
    return proposals


def _backup_current_params(params_path: Path | None = None) -> None:
    params_path = params_path or DEFAULT_PARAMS_PATH
    if not params_path.exists():
        return
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (BACKUPS_DIR / f"strategy_params_{ts}.json").write_text(params_path.read_text())


def _record_strategy_version(
    proposal_id: str,
    strategy_name: str,
    previous: dict[str, Any],
    applied: dict[str, Any],
    params_path: Path,
) -> None:
    """Append-only registry of every admin-approved production parameter change."""
    try:
        registry = Path(params_path).parent / "strategy_versions.jsonl"
        entry = {
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "proposal_id": proposal_id,
            "strategy_name": strategy_name,
            "previous_params": previous,
            "applied_params": applied,
            "params_sha256": hashlib.sha256(Path(params_path).read_bytes()).hexdigest(),
        }
        with registry.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning("could not record strategy version (%s)", type(exc).__name__)


def apply_proposal(proposal_id: str, params_path: Path | None = None) -> bool:
    """The explicit human-approval step: writes a previously-backtested
    proposal's parameters into `data/strategy_params.json` — backing up the
    prior version first — which is what actually changes what the live bot
    signals on its next scan. Refuses to apply a proposal that failed the
    acceptance criteria, or that doesn't exist. `params_path` overrides the
    default file (mainly for test isolation)."""
    proposal = load_proposal(proposal_id)
    if proposal is None:
        logger.error("no such proposal: %s", proposal_id)
        return False
    if proposal.get("passed") is not True:
        logger.error(
            "refusing to apply proposal %s: it did not pass acceptance criteria", proposal_id
        )
        return False

    params_path = params_path or DEFAULT_PARAMS_PATH
    strategy_name = proposal.get("strategy_name")
    overrides = proposal.get("param_overrides")
    if strategy_name not in PARAM_CLASSES or not _valid_proposal_overrides(strategy_name, overrides):
        logger.error("refusing to apply proposal %s: invalid strategy parameters", proposal_id)
        return False
    if proposal.get("strategy_version") != _strategy_version(strategy_name, params_path):
        logger.error("refusing to apply proposal %s: strategy code or baseline params changed", proposal_id)
        return False
    cls = PARAM_CLASSES[strategy_name]
    current = get_strategy_params(strategy_name, path=params_path)
    merged = {**asdict(current), **overrides}
    new_params = cls(**{f.name: merged[f.name] for f in fields(cls)})

    _backup_current_params(params_path)
    save_strategy_params(strategy_name, new_params, path=params_path)
    _record_strategy_version(proposal_id, strategy_name, asdict(current), asdict(new_params), params_path)
    logger.info("applied proposal %s to %s: %s", proposal_id, strategy_name, proposal["param_overrides"])
    return True


def _format_report(candidates: list[EvolutionCandidate]) -> str:
    lines = [
        f"{'ID':<10} {'OOS PASS':<9} {'Train Sharpe':>12} {'OOS avg R':>10} "
        f"{'OOS Trades':>10}  Overrides"
    ]
    for c in candidates:
        oos = c.out_of_sample_result
        lines.append(
            f"{c.proposal_id:<10} {'YES' if c.passed else 'no':<9} {c.result.sharpe_ratio:>12.2f} "
            f"{oos.avg_realized_r:>10.3f} {oos.total_trades:>10d}  {c.param_overrides}"
        )
    return "\n".join(lines)


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", choices=sorted(PARAM_CLASSES), help="strategy to evolve")
    parser.add_argument("--symbol", help="canonical symbol, e.g. EURUSD, XAUUSD")
    parser.add_argument("--timeframe", default="1h", help="timeframe, e.g. 1h, 4h, 15m")
    parser.add_argument("--grid", type=json.loads, default={}, help='JSON param grid, e.g. \'{"rsi_length": [10, 14, 20]}\'')
    parser.add_argument("--lookback-months", type=int, default=6)
    parser.add_argument("--apply", metavar="PROPOSAL_ID", help="apply a previously-backtested proposal by ID")
    parser.add_argument("--list", action="store_true", help="list saved proposals")
    args = parser.parse_args()

    if args.apply:
        ok = apply_proposal(args.apply)
        raise SystemExit(0 if ok else 1)

    if args.list:
        for p in list_proposals():
            print(f"{p['proposal_id']}  {p['strategy_name']:<16} pass={p['passed']}  {p['metrics']}")
        return

    if not args.strategy or not args.symbol:
        parser.error("--strategy and --symbol are required unless using --apply or --list")

    candidates = evolve_strategy(
        args.strategy, args.symbol, args.timeframe, args.grid, lookback_months=args.lookback_months
    )
    if not candidates:
        print("No candidates produced a backtest result (insufficient data?).")
        return
    print(_format_report(candidates))
    print(f"\nTo apply a passing proposal: python strategy_evolver.py --apply <ID>")


if __name__ == "__main__":
    _main()
