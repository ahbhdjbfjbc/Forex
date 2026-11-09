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
Every 1h: watchlist_store's symbols  ->  Scanner 1 -> Scanner 2  ->  (high-confidence only)  ->  Notifier
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

> **Live-data limitation:** The implemented `yfinance` adapter returns OHLCV
> bars that may be historical or delayed; freshness checks do not prove a
> real-time quote. No verified real-time provider adapter is currently
> implemented, so the publication gate blocks new recommendations. Outcome
> tracking is an OHLCV-based price estimate, not a broker execution or realized
> profit statement. Passing local tests does not prove a production deployment.

## What each piece does

- **`src/scanner_1_regime.py`** — pulls historical/delayed OHLCV (via `yfinance`, resampled to
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
  including a chart and caption in one Telegram `sendPhoto` request. The PNG
  and the exact analyzed OHLCV snapshot are retained under `data/charts/` and
  referenced by the persistent signal record. Discord/desktop remain optional
  text channels.

- **`src/telegram_bot.py`** — on-demand scanning: message the bot any pair
  or ticker (`EURUSD`, `XAUUSD`, `GBPJPY`, `BTCUSD`, or aliases like `gold`/
  `cable`/`bitcoin`), or use `/scan <symbol>`, and it runs Scanner 1 ->
  Scanner 2 to produce an analysis report. Any candidate is withheld when the
  market is closed or the price feed is not verified real-time. Every symbol you scan is
  auto-added to the hourly auto-scan watchlist. Also handles `/watchlist`
  (list what's being auto-scanned), `/add <symbol>`, `/remove <symbol>`.
  Every command, message, and callback fails closed unless
  `TELEGRAM_ALLOWED_USER_ID` is set to the owner's numeric Telegram user ID.
  This applies in private chats and groups; group commands additionally require
  `TELEGRAM_CHAT_ID` to match that group. A chat ID or username alone never
  authorizes an admin action. `src/symbol_resolver.py` handles the free-text -> ticker mapping
  (including proxying gold/silver through `GC=F`/`SI=F` futures, since
  Yahoo has no retail XAUUSD ticker).

- **`src/watchlist_store.py`** — the dynamic watchlist the hourly scheduler
  scans: seeded with `EURUSD, GBPUSD, XAUUSD, USDJPY`, grown by `/add` and
  by every successful `/scan`, shrunk by `/remove`. Persisted to
  `data/watchlist.json` so a bot restart doesn't lose it.

- **`main.py`** — every `AUTO_SCAN_INTERVAL_SECONDS` (default 1h), scans
  `watchlist_store`'s symbols through Scanner 1 -> Scanner 2 and pushes a
  alert only for setups that are actively triggered, above
  `MIN_SIGNAL_CONFIDENCE` (default 0.72), and accepted by every publish gate — everything else,
  including any `LOW_LIQUIDITY_CHOP` regime, stays silent. Also serves the
  webhook bridge + the Telegram bot concurrently in the same asyncio event
  loop. If `ENABLE_PAPER_TRADING=true`, each dispatched signal is also
  mirrored to an OANDA practice account.

- **`strategies/`** — each strategy (`trend_pullback.py`, `mean_reversion.py`,
  `breakout.py`) is a small class plus a frozen dataclass of its tunable
  parameters (RSI levels, EMA periods, ATR multipliers, ...). Parameters
  live in `data/strategy_params.json` (`src/strategy_params.py`), not
  hardcoded — `scanner_2_strategy.py` reads them fresh on every scan.

- **`backtester.py`** — a walk-forward simulation that calls the same
  Scanner 1/2 logic bar by bar, without lookahead (a signal computed on bar
  `i`'s close is simulated at bar `i+1`'s open). It computes simulated Win Rate,
  Profit Factor, Sharpe Ratio, Max Drawdown, and R-multiple; it does not report
  actual broker fills or realized account P&L.

- **`strategy_evolver.py`** — a script (not an autonomous background loop)
  that evaluates parameter variants on an earlier 70% chronological training
  window and validates on a later 30% holdout, with the same warm-up bars and
  live scanner logic. Holdout scores never rank candidates. A proposal passes
  only with at least 20 OOS trades, training and holdout Sharpe > 1.5, Profit
  Factor > 1.3, Max Drawdown < 12%, and higher OOS average R than the current
  baseline. Proposal files include window metrics and hashes for the strategy
  source and baseline parameters. Parameters change only after a separate
  explicit `/apply <id>` action; stale or failing proposals are refused, and
  every apply is preceded by a backup in `data/strategy_params_backups/`.
  Telegram supports `/evolve <strategy> <symbol> [timeframe]` and `/apply <id>`.

- **`src/ai_assistant.py`** — optional review of closed recommendations.
  With `AI_REVIEW_ENABLED=true`, the bot sends a rate-limited private report to
  the configured numeric admin. Local statistics require no model or paid
  service. Remote review is opt-in and requires `AI_API_BASE_URL`, `AI_API_KEY`,
  and `AI_MODEL`; only aggregate historical outcomes are sent over HTTPS.
  Reports record a prompt version and model and are saved under
  `data/ai_reviews/`. The assistant cannot change parameters; any proposed
  change must still pass the OOS workflow and be manually approved.

- **`src/system_health.py` / `src/admin_audit.py`** — persist the health state
  of the market-data and economic-calendar services, privately alert the
  numeric admin only on failures/recoveries, retry undelivered alerts, and
  write redacted Telegram command/callback audit events to
  `data/admin_audit.jsonl` without storing message arguments.

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
   includes `bar_time` and sends price updates while BUY/SELL conditions remain
   active so the bot can monitor TP/SL; the server suppresses duplicate posts).
   It sends `NONE` once when the setup clears, allowing a later fresh signal in
   the same direction. `alertcondition()` alerts are also de-duplicated, but do
   not include `bar_time` or send the reset event.
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
- Mutable watchlist/strategy files remain JSON under `data/`; open
  recommendations and their one-shot outcome outbox are stored in
  `data/signal_lifecycle.sqlite3` (or `FOREX_DATA_DIR`). Mount persistent storage
  for this directory in production so active recommendations survive redeploys.
- Signal results are estimates from OHLCV price touches, not broker fills.
  The Telegram recommendation shows one final target; internal TP1 progress is
  retained for lifecycle tracking and the recommendation stays open until TP2 or SL.
  If one live candle touches both SL and TP, the outcome is recorded as
  `AMBIGUOUS` with no exit price, points, or R-multiple; OHLCV cannot reveal
  which level came first. The historical backtester uses stop-first only as a
  conservative simulation assumption. Yahoo Finance OHLCV has no Bid/Ask, so
  spread and slippage are not included and this limitation is shown in the
  result message.
- The backtester's equity curve is realized-P&L-only (marked to market at
  trade close, not intra-trade), and only TP1 is simulated as an exit
  target — see the module docstring in `backtester.py` for the full list of
  simplifications and why each one is honest rather than optimistic.
- `strategy_evolver.py` never applies a parameter change on its own —
  `apply_proposal(id)` is always an explicit, separate call you make.
