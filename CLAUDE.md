# Forex Scanner & Signal Engine — Project Standards

## What this is
A two-stage Forex market scanner: Scanner 1 classifies market regime per
symbol/timeframe, Scanner 2 matches a strategy to that regime and emits
concrete trade signals. A FastAPI bridge accepts TradingView webhook alerts,
a notifier dispatches formatted alerts to Telegram/Discord, an on-demand
Telegram bot lets you type any ticker (EURUSD, XAUUSD, BTCUSD, ...) for an
immediate scan, and a background scheduler auto-scans a dynamic watchlist
every few hours, pushing only high-confidence, actively-triggered setups.

On top of that, a strategy evolution & backtesting module
(`backtester.py` + `strategy_evolver.py`) lets you test parameter variants
of a strategy against historical data before ever touching what's live, and
an opt-in paper-trading bridge (`src/oanda_client.py`) can mirror dispatched
signals to an OANDA practice account.

## Directory layout

```
forex_scanner_bot/
├── CLAUDE.md
├── .env                        # secrets — never commit (see .env.example)
├── config.py                   # typed Settings loaded from .env
├── main.py                     # entry point: scheduler + webhook server + Telegram bot
├── backtester.py                # walk-forward backtesting engine (reuses live scanner code)
├── strategy_evolver.py          # propose/backtest/apply parameter variants (a script, not a live loop)
├── data/                        # gitignored, auto-created — the project's only durable state
│   ├── watchlist.json            # dynamic auto-scan watchlist
│   ├── strategy_params.json      # tunable strategy parameters (the live source of truth)
│   ├── strategy_proposals/       # backtested candidates from strategy_evolver.py
│   └── strategy_params_backups/  # snapshot taken before every apply_proposal()
├── strategies/
│   ├── base.py                  # Strategy ABC + finalize_signal() (shared R:R/risk-sizing logic)
│   ├── trend_pullback.py        # TrendPullbackStrategy + TrendPullbackParams
│   ├── mean_reversion.py        # MeanReversionStrategy + MeanReversionParams
│   └── breakout.py              # BreakoutStrategy + BreakoutParams
├── src/
│   ├── models.py                # shared pydantic/dataclass schemas
│   ├── data_provider.py         # OHLCV fetch (yfinance / tradingview_ta), retries
│   ├── indicators.py            # pure indicator math (ADX, EMA, ATR, BB, RSI, Donchian, Keltner)
│   ├── session_utils.py         # forex session detection + RVOL
│   ├── risk.py                   # pip/unit value, lot/position sizing, R:R math
│   ├── symbol_resolver.py        # free-text ticker parsing/aliasing (gold -> XAUUSD, etc.)
│   ├── watchlist_store.py        # file-backed dynamic watchlist for the auto-scan scheduler
│   ├── strategy_params.py        # loads/saves data/strategy_params.json
│   ├── ast_patcher.py            # surgical constant-patching (secondary to strategy_params.py)
│   ├── oanda_client.py           # OANDA v20 REST wrapper — practice endpoint only
│   ├── paper_executor.py         # opt-in bridge: TradeSignal -> simulated OANDA order
│   ├── scanner_1_regime.py      # regime classification engine
│   ├── scanner_2_strategy.py    # strategy dispatcher (delegates to strategies/*)
│   ├── tradingview_bridge.py    # FastAPI webhook receiver
│   ├── notifier.py              # Telegram/Discord/desktop push-alert dispatcher
│   └── telegram_bot.py          # on-demand Telegram listener + scan-report formatter + commands
├── pine_script/
│   └── tv_indicator.pine        # Pine Script v5 template
└── tests/
    ├── mock_data.py              # synthetic OHLCV generators per regime
    ├── test_scanners.py          # dry-run validation for Scanner 1 & 2, notifier, bridge
    └── test_evolution_module.py  # strategies/params, ast_patcher, backtester, evolver, OANDA (all mocked)
```

## Module boundaries (do not blur these)

| Module | Owns | Must NOT do |
|---|---|---|
| `src/data_provider.py` | Fetching/caching OHLCV from yfinance / tradingview_ta, retries, rate-limit handling | Any indicator math or regime logic |
| `src/indicators.py` | Pure indicator functions — stateless, numpy/pandas in, values out | Fetching data, knowing about "regime" or "strategy" |
| `src/session_utils.py` | Forex session (Asian/London/NY/overlap) + RVOL calc | Indicator math |
| `src/risk.py` | Pip/unit value, lot/position sizing, R:R math | Regime/strategy logic |
| `src/symbol_resolver.py` | Free-text -> canonical symbol (aliases, slashes, case), canonical -> yfinance ticker, instrument type (FX/METAL/CRYPTO) | Fetching data, indicator/regime/strategy logic |
| `src/watchlist_store.py` | The dynamic, file-backed set of symbols the auto-scan scheduler tracks; add/remove/list, thread-safe | Regime/strategy/indicator logic, Telegram I/O |
| `src/strategy_params.py` | Load/save `data/strategy_params.json`; merge in-memory overrides for backtesting | Regime/strategy math itself |
| `src/ast_patcher.py` | Minimal-diff, byte-range patching of a literal constant in a `.py` file, located via `ast` | Anything strategy/parameter-aware — it only knows about Python syntax, not what a value means |
| `src/models.py` | Shared schemas (`MarketRegime`, `RegimeResult`, `TradeSignal`, webhook payload schema) | Business logic |
| `strategies/*.py` | One strategy's entry-trigger logic + its tunable-params dataclass | Regime classification, risk sizing beyond calling `finalize_signal()`, fetching data |
| `src/scanner_1_regime.py` | Classify each (symbol, timeframe) into one `MarketRegime` | Signal generation, notification, I/O to Telegram |
| `src/scanner_2_strategy.py` | Map a `RegimeResult` to a strategy class (`strategies/`), load its current params, delegate | Any strategy math itself (lives in `strategies/*.py`), fetching raw data |
| `backtester.py` | Walk-forward simulation over historical OHLCV, calling the *same* Scanner 1/2 code as live, no lookahead; trade/equity metrics | Reimplementing strategy logic separately, writing to `data/strategy_params.json` |
| `strategy_evolver.py` | Grid-search parameter variants via `backtester.py`, save proposals, and (only on explicit `apply_proposal` call) write a passing variant to `data/strategy_params.json` | Auto-applying anything on its own; strategy/indicator math itself |
| `src/oanda_client.py` | OANDA v20 REST calls — hard-locked to the practice/demo endpoint | Ever construct a live-money URL; any trading logic (just places what it's told) |
| `src/paper_executor.py` | Opt-in bridge from a dispatched `TradeSignal` to `oanda_client.place_market_order` | Run unless `ENABLE_PAPER_TRADING=true`; touch on-demand `/scan` signals (auto-scan only) |
| `src/tradingview_bridge.py` | FastAPI app: validate + parse incoming TradingView webhook JSON, hand off to orchestrator | Indicator math, notification formatting |
| `src/notifier.py` | Format + send push alerts (Telegram Bot API, Discord webhook, desktop) for scanner/webhook-generated `TradeSignal`s | Any trading logic |
| `src/telegram_bot.py` | On-demand Telegram polling listener: sanitize incoming text/commands, run a fresh scan via Scanner 1/2, format + reply with the interactive "SCAN REPORT"; `/watchlist`, `/add`, `/remove`, `/paper_status` | Indicator/regime/strategy math itself; must only call the public Scanner 1/2 entry points |
| `main.py` | Wire everything together: scheduler auto-scanning `watchlist_store`'s symbols every `AUTO_SCAN_INTERVAL_SECONDS`, orchestrate Scanner 1 → Scanner 2 → notifier (gated by `MIN_SIGNAL_CONFIDENCE`) → optional paper execution, run the webhook bridge + Telegram bot concurrently | Indicator/regime/strategy math itself |
| `config.py` | Load `.env` into a typed `Settings` object (watchlist, risk %, ports, keys) | Hold secrets as literals — always via `.env` |

Cross-module calls should go through these public entry points only:
- `scanner_1_regime.scan_regime(symbol, timeframe) -> RegimeResult`
- `scanner_1_regime.scan_regime_with_data(symbol, timeframe, settings, fetch_symbol=None) -> (RegimeResult, DataFrame) | None`
- `scanner_1_regime.scan_symbols_with_data(symbols, settings) -> list[(RegimeResult, DataFrame)]` — arbitrary symbol list (used by the auto-scan scheduler over `watchlist_store`)
- `scanner_2_strategy.generate_signal(regime_result, df, settings, param_overrides=None) -> TradeSignal | None`
- `notifier.send_alert(signal: TradeSignal)` — push alerts
- `telegram_bot.run_on_demand_scan(raw_text, settings) -> str` — on-demand scan report
- `telegram_bot.run_evolution(strategy_name, symbol, timeframe, settings) -> str` — `/evolve` report; formats the best (highest-Sharpe) candidate from `evolver.DEFAULT_GRIDS[strategy_name]`, never applies anything itself (`/apply <id>` is the separate, explicit step)
- `watchlist_store.get_all() / .add(raw) / .remove(raw) -> (symbol, changed: bool)`
- `backtester.run_backtest(symbol, timeframe, settings, param_overrides=None, lookback_months=6) -> BacktestResult | None`
- `strategy_evolver.evolve_strategy(strategy_name, symbol, timeframe, param_grid, settings) -> list[EvolutionCandidate]` and `.apply_proposal(proposal_id) -> bool`
- `paper_executor.execute_paper_trade(signal, settings) -> bool`

`scan_regime_with_data`'s `fetch_symbol` param decouples "what ticker to
fetch" from "what symbol this is" — needed because Yahoo Finance has no
retail spot ticker for gold/silver (proxied via `GC=F`/`SI=F` futures) while
`RegimeResult.symbol`/`TradeSignal.symbol` must stay the canonical form
(`XAUUSD`) for `symbol_resolver.instrument_type` and display to work.

### Why the backtester reuses live code instead of a separate engine
`backtester.py` deliberately calls the exact same
`scanner_1_regime.classify_regime`/`_build_metrics` and
`scanner_2_strategy.generate_signal` that run live, walking bar-by-bar with
`df.iloc[:i+1]` slices (no lookahead) instead of adopting vectorbt/
backtesting.py's own strategy DSL. A backtest that runs *different* logic
than the live bot is worse than no backtest — it creates false confidence,
since a passing result says nothing about what the live bot actually does.
The only way to guarantee they match is to literally call the same functions.

### Two ways to change a strategy parameter — prefer the first
1. **`data/strategy_params.json`** (via `src/strategy_params.py`) — the
   normal path. `strategy_evolver.py` mutates this (only through
   `apply_proposal`, only after a passing backtest, only on your explicit
   call) and it's what `scanner_2_strategy.py` reads on every scan.
2. **`src/ast_patcher.py`** — only for a literal constant that hasn't been
   exposed as a JSON parameter yet (e.g. a module-level threshold in
   `src/indicators.py`). Minimal-diff (byte-range replacement located via
   `ast`, not a full-file regenerate), but it's editing code, so treat it as
   a manual, reviewed operation — not something `strategy_evolver.py` calls
   as part of its automated search.

### Strategy evolution is human-gated, not autonomous
`strategy_evolver.py` is a script you run on demand (CLI: `python
strategy_evolver.py --strategy ... --symbol ... --grid '...'`, or via
Telegram: `/evolve <strategy> <symbol> [timeframe]` using
`evolver.DEFAULT_GRIDS`), not a background loop. It never writes to
`data/strategy_params.json` except inside `apply_proposal(proposal_id)`,
which you call explicitly by ID after reviewing the backtest report (CLI
`--apply <id>` or Telegram `/apply <id>`) — and it refuses to apply a
proposal that didn't pass the acceptance criteria (Sharpe > 1.5, Profit
Factor > 1.3, Max Drawdown < 12%, see `strategy_evolver.ACCEPTANCE_MIN_*`
constants). Every apply is preceded by a timestamped backup in
`data/strategy_params_backups/`. The Telegram `/evolve` report never claims
a variant was applied ("Approved & Updated") unless `/apply` was actually
called separately and succeeded — a passing backtest only ever earns an
invitation to run `/apply <id>`, never an automatic status change.

### Why this architecture is cheap to work in, for a human or for Claude Code
1. **Parameter changes are a JSON edit, not a code edit.** Most "tune the
   strategy" requests reduce to changing a handful of values in
   `data/strategy_params.json` — a small, structured diff, not a rewrite of
   `strategies/*.py`.
2. **Each strategy is an isolated file.** Changing `trend_pullback`'s entry
   logic means opening `strategies/trend_pullback.py` — not the regime
   classifier, the risk math, or the other two strategies.
3. **The backtester is a fixed-threshold check, not a comparison to the
   live baseline, and it never writes anything.** `passes_acceptance_criteria()`
   tests a candidate against fixed bars (Sharpe > 1.5, PF > 1.3, MaxDD < 12%)
   — passing means "cleared the bar," not "better than what's currently
   live," and a strategy_evolver.py run stops at a proposal file regardless
   of the result. The only path from "backtest passed" to "live behavior
   changed" is the human calling `apply_proposal`/`/apply <id>` explicitly.
   Don't describe this pipeline as auto-applying improvements — it doesn't,
   by design.

## Coding standards
- Python 3.11+. Type hints on every function signature, no bare `Any` unless
  truly dynamic (e.g. raw webhook JSON before validation).
- Use `pydantic.BaseModel` for anything crossing a process/network boundary
  (webhook payloads, config). Use `@dataclass` for internal-only value objects
  that never get (de)serialized.
- No global mutable state. Pass `Settings` explicitly; don't read `os.environ`
  outside `config.py`. The one deliberate exception is `src.watchlist_store`'s
  module-level singleton — the scheduler thread and the Telegram asyncio loop
  both need to see the same watchlist, so it's a lock-guarded, file-backed
  singleton rather than something threaded through every call site.
- Every external call (data provider, Telegram, Discord, webhook parsing) must
  handle: connection errors, timeouts, rate limits (HTTP 429), and missing/NaN
  OHLC data — fail soft (return `None` / log + skip that symbol), never crash
  the scan loop for one bad symbol.
- Logging via stdlib `logging`, module-level logger, no `print()` in library
  code (`print` is fine in dry-run/test scripts).
- No comments explaining *what* code does — only *why*, and only when the
  reason isn't obvious from reading it.
- `src/` modules are imported as `from src.module_name import ...` — `main.py`
  and `config.py` stay at the project root since they are entry points.

## Environment
- Copy `.env.example` to `.env` and fill in real values before running.
- `config.py` is the single place that reads `.env` (via `pydantic-settings`).
- Never commit `.env`. `.gitignore` already excludes it.
- Install deps: `pip install -r requirements.txt`.
- Run a dry-run scan (mock data, no network/Telegram calls):
  `python -m tests.test_scanners` or `pytest tests/ -v`
- Run the webhook bridge: `uvicorn src.tradingview_bridge:app --port 8000`
- Run the full orchestrator loop: `python main.py`
- Backtest a symbol/timeframe: `python -c "from backtester import run_backtest; print(run_backtest('EURUSD', '1h'))"`
- Evolve a strategy's parameters: `python strategy_evolver.py --strategy trend_pullback --symbol EURUSD --timeframe 1h --grid '{"rsi_bullish_ceiling": [50, 55, 60]}'`, then `python strategy_evolver.py --apply <proposal_id>`

## Testing
- `tests/mock_data.py` generates synthetic OHLCV for each regime type so
  Scanner 1 and Scanner 2 can be verified without live API access.
- `tests/test_evolution_module.py` covers `strategies/`, `src/strategy_params.py`,
  `src/ast_patcher.py`, `backtester.py`, `strategy_evolver.py`, and
  `src/oanda_client.py`/`src/paper_executor.py` — all network calls (data
  fetch, OANDA HTTP) are mocked; nothing there touches the real `data/` dir.
- Run tests: `pytest tests/ -v`
- Any new strategy or regime rule must ship with a mock-data test case that
  proves it triggers under the intended condition and does NOT trigger under
  adjacent conditions.

## Non-goals (explicitly out of scope unless asked)
- No real-money order execution / live broker integration. `src/oanda_client.py`
  only ever talks to OANDA's practice/demo endpoint (hard-coded, refuses to
  run otherwise) — this is simulated paper trading, not live trading, and
  it's opt-in (`ENABLE_PAPER_TRADING=false` by default).
- No relational database. `data/` holds plain JSON files (watchlist,
  strategy params, backtest proposals, param backups) — not a DB.
  Regime metrics and signals are still computed fresh per scan cycle.
- `strategy_evolver.py` never applies a parameter change autonomously — see
  "Strategy evolution is human-gated" above.
