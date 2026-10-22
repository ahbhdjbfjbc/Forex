# Forex Scanner & Signal Engine

A two-stage Forex market scanner with TradingView webhook integration and an
on-demand Telegram bot.

```
Scanner 1 (regime)  ->  Scanner 2 (strategy/signal)  ->  Notifier (Telegram/Discord)
                                                              ^
TradingView Pine alert  ->  tradingview_bridge (FastAPI)  ---'

Telegram: type "EURUSD" / "gold" / "/scan BTCUSD"  ->  telegram_bot.py  ->  Scanner 1 -> Scanner 2  ->  reply
                                                             |
                                                    auto-adds to watchlist_store
                                                             v
Every 3h: watchlist_store's symbols  ->  Scanner 1 -> Scanner 2  ->  (high-confidence only)  ->  Notifier
                                                                                                     |
                                                                                    ENABLE_PAPER_TRADING=true
                                                                                                     v
                                                                                    OANDA practice account (paper_executor)

strategy_evolver.py  ->  backtester.py (reuses live Scanner 1/2 code)  ->  proposal file  ->  [you review]  ->  apply_proposal(id)  ->  data/strategy_params.json
```

See [CLAUDE.md](CLAUDE.md) for module boundaries and coding standards.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your real values
```

## Run

```bash
# Dry-run tests against synthetic data (no network, no API keys needed)
pytest tests/ -v

# Full orchestrator: scheduled scan loop + TradingView webhook server
python main.py

# Webhook bridge only (e.g. for local TradingView alert testing)
uvicorn src.tradingview_bridge:app --host 0.0.0.0 --port 8000

# Backtest a symbol/timeframe against the current live strategy params
python -c "from backtester import run_backtest; r = run_backtest('EURUSD', '1h'); print(r)"

# Search a parameter grid, backtest each variant, review, then apply one
python strategy_evolver.py --strategy trend_pullback --symbol EURUSD --timeframe 1h \
    --grid '{"rsi_bullish_ceiling": [50, 55, 60], "atr_sl_multiplier": [1.2, 1.5, 1.8]}'
python strategy_evolver.py --apply <proposal_id>   # only works if that proposal passed
```

See [Railway deployment notes](RAILWAY_DEPLOYMENT_AR.md) for hosted deployment,
environment variables, persistent storage, and health-check settings.

## What each piece does

- **`src/scanner_1_regime.py`** — pulls OHLCV (via `yfinance`, resampled to
  4H/1H/15M as needed) for every symbol in `WATCHLIST`, computes ADX, EMA
  20/50/200 alignment, swing structure, ATR expansion, Bollinger bandwidth,
  RVOL, and active FX session, then classifies each (symbol, timeframe) pair
  into one of `STRONG_TREND_BULLISH`, `STRONG_TREND_BEARISH`,
  `RANGING_CONSOLIDATION`, `HIGH_VOLATILITY_EXPANSION`, `LOW_LIQUIDITY_CHOP`.

- **`src/scanner_2_strategy.py`** — maps each regime to its matched
  strategy (trend pullback / mean-reversion fade / volatility breakout /
  filtered out), checks whether an entry trigger is *currently* active
  (not just "regime is right"), and if so produces exact entry/SL/TP1/TP2
  and a lot-size recommendation via `src/risk.py`.

- **`src/tradingview_bridge.py`** — FastAPI webhook at
  `POST /webhook/tradingview` for externally-triggered Pine Script alerts
  (see `pine_script/tv_indicator.pine`). Validates the JSON schema and a
  shared secret, converts the alert into the same `TradeSignal` shape
  Scanner 2 produces, and forwards it to the notifier.

- **`src/notifier.py`** — formats and sends the trade alert to Telegram,
  Discord, and/or desktop notification (macOS), whichever are configured
  in `.env`.

- **`src/telegram_bot.py`** — on-demand scanning: message the bot any pair
  or ticker (`EURUSD`, `XAUUSD`, `GBPJPY`, `BTCUSD`, or aliases like `gold`/
  `cable`/`bitcoin`), or use `/scan <symbol>`, and it runs Scanner 1 ->
  Scanner 2 fresh and replies with a "SCAN REPORT" — either a full trade
  setup or a "DO NOT TRADE" with the reason. Every symbol you scan is
  auto-added to the 3-hour auto-scan watchlist. Also handles `/watchlist`
  (list what's being auto-scanned), `/add <symbol>`, `/remove <symbol>`.
  Only the chat configured as `TELEGRAM_CHAT_ID` is served. Private chats
  work without an extra user setting; if commands are used in a group, set
  `TELEGRAM_ALLOWED_USER_ID` to the one numeric Telegram user ID allowed to
  issue commands. A group chat alone no longer grants command access to every
  member. `src/symbol_resolver.py` handles the free-text -> ticker mapping
  (including proxying gold/silver through `GC=F`/`SI=F` futures, since
  Yahoo has no retail XAUUSD ticker).

- **`src/watchlist_store.py`** — the dynamic watchlist the 3-hour scheduler
  scans: seeded with `EURUSD, GBPUSD, XAUUSD, USDJPY`, grown by `/add` and
  by every successful `/scan`, shrunk by `/remove`. Persisted to
  `data/watchlist.json` so a bot restart doesn't lose it.

- **`main.py`** — every `AUTO_SCAN_INTERVAL_SECONDS` (default 3h), scans
  `watchlist_store`'s symbols through Scanner 1 -> Scanner 2 and pushes a
  Telegram/Discord alert only for setups that are both actively triggered
  *and* at or above `MIN_SIGNAL_CONFIDENCE` (default 0.6) — everything else,
  including any `LOW_LIQUIDITY_CHOP` regime, stays silent. Also serves the
  webhook bridge + the Telegram bot concurrently in the same asyncio event
  loop. If `ENABLE_PAPER_TRADING=true`, each dispatched signal is also
  mirrored to an OANDA practice account.

- **`strategies/`** — each strategy (`trend_pullback.py`, `mean_reversion.py`,
  `breakout.py`) is a small class plus a frozen dataclass of its tunable
  parameters (RSI levels, EMA periods, ATR multipliers, ...). Parameters
  live in `data/strategy_params.json` (`src/strategy_params.py`), not
  hardcoded — `scanner_2_strategy.py` reads them fresh on every scan.

- **`backtester.py`** — a walk-forward engine that calls the *exact same*
  Scanner 1/2 code that runs live, bar by bar, with no lookahead (a signal
  computed on bar `i`'s close fills at bar `i+1`'s open). Computes Win Rate,
  Profit Factor, Sharpe Ratio, Max Drawdown, and realized R-multiple from
  the resulting trades.

- **`strategy_evolver.py`** — a script (not a background loop) that
  backtests a grid of parameter variants for one strategy, saves each as a
  proposal under `data/strategy_proposals/`, and only writes to
  `data/strategy_params.json` — changing what the live bot actually signals
  — when you explicitly call `apply_proposal(<id>)` on a variant that passed
  the acceptance criteria (Sharpe > 1.5, Profit Factor > 1.3, Max Drawdown
  < 12%). Every apply is preceded by a backup in
  `data/strategy_params_backups/`. Also reachable from Telegram:
  `/evolve <strategy> <symbol> [timeframe]` runs a built-in parameter grid
  and reports the best candidate (a "🧪 STRATEGY EVOLUTION REPORT" with Win
  Rate/Profit Factor/Sharpe/Max Drawdown), and `/apply <proposal_id>`
  applies it — the report only ever invites you to `/apply`, it never
  claims a change was made until you actually make that separate call.

- **`src/ast_patcher.py`** — a secondary, rarely-needed mechanism: surgically
  patches a single literal constant inside a `.py` file (located via `ast`,
  replaced by exact byte range — every other line stays untouched) for the
  case where a value hasn't been exposed as a JSON parameter yet. Prefer
  `data/strategy_params.json` for anything already parameterized.

- **`src/oanda_client.py` / `src/paper_executor.py`** — opt-in
  (`ENABLE_PAPER_TRADING=false` by default) paper trading via OANDA's v20
  REST API. `oanda_client.py` is hard-locked to OANDA's practice/demo
  endpoint and refuses to run against anything else. `paper_executor.py`
  bridges a dispatched auto-scan `TradeSignal` to a simulated market order
  with attached SL/TP — never wired into on-demand `/scan` requests, so
  poking the bot never opens a demo position by accident. Check status with
  the `/paper_status` Telegram command.

## Configuring TradingView alerts

1. Add `pine_script/tv_indicator.pine` as an indicator on your chart.
2. Create an alert on "Any alert() function call" (recommended — the script
   already builds the JSON payload for you), or on one of the
   `alertcondition()`s if you prefer to compose the message by hand.
3. For public use, set the webhook URL to `https://<your-domain>/webhook/tradingview`.
   Put Uvicorn behind an HTTPS-terminating reverse proxy on port 443; do not
   expose the plain-HTTP Uvicorn port directly to the internet. The app applies
   an in-process limit of 120 requests per peer per 60 seconds, but keep
   proxy-level rate limits as well: the in-process counter is not shared across
   multiple workers/replicas and cannot replace edge-level abuse protection.
4. Set the `webhookSecret` input on the indicator to the same unique random
   value as `WEBHOOK_SHARED_SECRET` in your `.env`. Never use the indicator's
   placeholder value; the API rejects it.

## Known limitations (by design, not oversights)

- Pip-value conversion to USD is exact for USD-quoted majors and USDJPY;
  it's an approximation for cross pairs (e.g. EURJPY) — see the docstring
  in `src/risk.py`.
- Metals/crypto don't have a standard FX "pip"/100k-unit-lot convention, so
  position sizing for those is reported as raw units (oz, BTC, ...) via
  `risk.calculate_unit_size`, not FX lots.
- Gold/silver are proxied through Yahoo Finance futures (`GC=F`/`SI=F`) —
  there's no retail spot XAUUSD ticker on that data source.
- No real-money order execution — `src/oanda_client.py` only ever talks to
  OANDA's practice/demo endpoint; this is simulated paper trading, opt-in
  and off by default.
- No relational database — `data/` holds plain JSON files (watchlist,
  strategy params, backtest proposals, param backups); everything else
  (metrics, signals) is computed fresh each scan cycle.
- The backtester's equity curve is realized-P&L-only (marked to market at
  trade close, not intra-trade), and only TP1 is simulated as an exit
  target — see the module docstring in `backtester.py` for the full list of
  simplifications and why each one is honest rather than optimistic.
- `strategy_evolver.py` never applies a parameter change on its own —
  `apply_proposal(id)` is always an explicit, separate call you make.
