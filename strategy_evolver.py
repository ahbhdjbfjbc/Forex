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

from config import Settings, get_settings
from backtester import BacktestResult, run_backtest
from src.strategy_params import DEFAULT_PARAMS_PATH, PARAM_CLASSES, get_strategy_params, save_strategy_params
from src.storage import get_data_dir

logger = logging.getLogger(__name__)

PROPOSALS_DIR = get_data_dir() / "strategy_proposals"
BACKUPS_DIR = get_data_dir() / "strategy_params_backups"

ACCEPTANCE_MIN_SHARPE = 1.5
ACCEPTANCE_MIN_PROFIT_FACTOR = 1.3
ACCEPTANCE_MAX_DRAWDOWN_PCT = 12.0

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
    passed: bool
    created_at: datetime


def _grid(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of {param: [values...]} -> list of override dicts.
    An empty grid means "just the current baseline, unmodified"."""
    if not param_grid:
        return [{}]
    keys = list(param_grid.keys())
    return [dict(zip(keys, combo)) for combo in itertools.product(*param_grid.values())]


def evolve_strategy(
    strategy_name: str,
    symbol: str,
    timeframe: str,
    param_grid: dict[str, list[Any]],
    settings: Settings | None = None,
    lookback_months: int | None = 6,
) -> list[EvolutionCandidate]:
    """Backtests every combination in `param_grid` (merged over the current
    baseline params for `strategy_name`), saves each as a proposal, and
    returns all candidates sorted best-Sharpe-first. Writes proposal files
    only — never touches `data/strategy_params.json`."""
    if strategy_name not in PARAM_CLASSES:
        raise ValueError(f"unknown strategy {strategy_name!r}; known: {sorted(PARAM_CLASSES)}")

    settings = settings or get_settings()
    candidates: list[EvolutionCandidate] = []

    for overrides in _grid(param_grid):
        result = run_backtest(
            symbol, timeframe, settings,
            param_overrides={strategy_name: overrides},
            lookback_months=lookback_months,
        )
        if result is None:
            logger.warning("skipping candidate %s: backtest returned no result", overrides)
            continue

        passed = result.passes_acceptance_criteria(
            ACCEPTANCE_MIN_SHARPE, ACCEPTANCE_MIN_PROFIT_FACTOR, ACCEPTANCE_MAX_DRAWDOWN_PCT
        )
        candidate = EvolutionCandidate(
            proposal_id=uuid4().hex[:8],
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            param_overrides=overrides,
            result=result,
            passed=passed,
            created_at=datetime.now(timezone.utc),
        )
        candidates.append(candidate)
        _save_proposal(candidate)

    candidates.sort(key=lambda c: c.result.sharpe_ratio, reverse=True)
    return candidates


def _proposal_payload(candidate: EvolutionCandidate) -> dict[str, Any]:
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
            "total_trades": r.total_trades,
            "win_rate_pct": r.win_rate_pct,
            "profit_factor": r.profit_factor,
            "sharpe_ratio": r.sharpe_ratio,
            "max_drawdown_pct": r.max_drawdown_pct,
            "avg_realized_r": r.avg_realized_r,
            "start": r.start.isoformat(),
            "end": r.end.isoformat(),
            "bars_tested": r.bars_tested,
        },
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
    cls = PARAM_CLASSES[strategy_name]
    current = get_strategy_params(strategy_name, path=params_path)
    merged = {**asdict(current), **overrides}
    new_params = cls(**{f.name: merged[f.name] for f in fields(cls)})

    _backup_current_params(params_path)
    save_strategy_params(strategy_name, new_params, path=params_path)
    logger.info("applied proposal %s to %s: %s", proposal_id, strategy_name, proposal["param_overrides"])
    return True


def _format_report(candidates: list[EvolutionCandidate]) -> str:
    lines = [f"{'ID':<10} {'PASS':<6} {'Sharpe':>7} {'PF':>6} {'MaxDD%':>7} {'WinRate%':>9} {'Trades':>7}  Overrides"]
    for c in candidates:
        r = c.result
        lines.append(
            f"{c.proposal_id:<10} {'YES' if c.passed else 'no':<6} {r.sharpe_ratio:>7.2f} "
            f"{r.profit_factor if isinstance(r.profit_factor, float) else 999:>6.2f} {r.max_drawdown_pct:>7.2f} "
            f"{r.win_rate_pct:>9.2f} {r.total_trades:>7d}  {c.param_overrides}"
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
