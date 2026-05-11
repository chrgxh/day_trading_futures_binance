# Day Trading Bot

Context file for Claude Code. Read this before suggesting changes or generating code.

## Project goal

A futures day trading bot that places trades on Binance Futures based on configurable strategies.

## Tech stack

- **Language:** Python 3.11+
- **Exchange API:** Binance Futures (via the official `python-binance` client)
- **Logging:** loguru
- **Notifications:** Resend (crash emails via `resend` SDK)
- **Runtime:** Docker (single container, `docker compose` for local dev)
- **Config:** `.env` for secrets, `config.yaml` for strategy parameters

## File layout

```
day-trading-bot/
├── bot.py                   # Entry point — bot loop, orchestration, risk controls
├── strategies.py            # Pluggable strategy functions + STRATEGIES registry
├── utils/
│   ├── __init__.py
│   ├── general.py           # Shared primitives — build_client, with_retry, round_price, send_crash_email, order normalizers
│   ├── account.py           # Account state layer — connection, balances, positions, symbol info, leverage, recent trades
│   ├── orders.py            # Regular orders — market, limit, tp_limit, get_open_orders, cancel, cancel_all
│   ├── algo_orders.py       # Conditional orders — stop/TP market and limit variants, cancel_algo
│   ├── positions.py         # Position management — close_position
│   ├── market.py            # Public market data (OHLCV, mark price)
│   ├── trade_manager.py     # Background trade state manager — monitors positions, reconciles orders on external fills
│   ├── pnl_reporter.py      # Daily P&L reporter — DailyPnLReporter daemon thread appends net P&L per symbol to logs/pnl.csv at UTC midnight
│   └── indicators.py        # Signal types (Signal, TradeSignal), raw indicators (SMA, EMA, MACD, ADX, RSI, resample_to_1h), interval_to_minutes
├── config.yaml              # Non-secret runtime config (symbols, intervals, risk limits)
├── .env                     # Secrets ONLY (API key, API secret) — never committed
├── .env.example             # Placeholder template, safe to commit
├── .env.testnet             # Testnet secrets for integration tests — never committed
├── .env.testnet.example     # Testnet placeholder template, safe to commit
├── .gitignore
├── .dockerignore
├── Dockerfile
├── docker-compose.yml       # Mounts ./logs into the container
├── requirements.txt
├── pytest.ini               # Registers integration marker; plain pytest skips integration tests
├── tests/
│   ├── unit/                # Fast unit tests — no network, run with: pytest tests/unit/
│   │   ├── test_indicators.py
│   │   ├── test_strategies.py
│   │   └── test_trade_manager.py
│   └── integration/         # Testnet integration tests — run with: pytest -m integration
│       ├── conftest.py      # Loads .env.testnet, client/sym_info/open_position fixtures
│       ├── test_account.py
│       ├── test_market.py
│       ├── test_orders.py
│       ├── test_algo_orders.py
│       ├── test_positions.py
│       ├── test_trade_manager.py
│       └── test_notifications.py
├── logs/                    # Log output; mounted volume, not baked into image
└── sandbox.ipynb            # Manual testnet notebook — runs all scenarios against the live testnet
```

## Hard rules

1. **Secrets never leave `.env`.** Do not hardcode API keys anywhere. `.env` must be in both `.gitignore` and `.dockerignore`. Secrets are passed at runtime via `env_file:` in `docker-compose.yml`.
2. **All Binance API calls go through `utils/account.py`, `utils/orders.py`, `utils/algo_orders.py`, `utils/positions.py`, or `utils/market.py`.** `bot.py`, `strategies.py`, and `utils/indicators.py` must not import `binance` directly.
3. **Futures only.** Use futures endpoints exclusively (`futures_*` methods on the client). No spot trading.
4. **Every API action and every bot decision is logged via loguru** to both stdout and a file in `logs/`. Logs must survive container restarts via a mounted volume.
5. **The log file is not in the Docker image.** It is created at runtime in the mounted volume.

## Design decisions

- **Testnet vs mainnet toggle.** `BINANCE_TESTNET=true` in `.env`. Default to testnet for safety.
- **Risk controls live in `bot.py`** — max position size, max daily loss, kill switch. Strategies decide intent; `bot.py` enforces limits before anything reaches `utils/account.py`. On every position open, `bot.py` places dual stop losses (stop-limit + stop-market via the algo API), a trailing take profit (`TRAILING_STOP_MARKET` via the regular order API), and a maker GTC limit TP at `risk.take_profit_limit_pct` (default 3%) above/below entry (via the regular order API). All four are cancelled explicitly on a CLOSE signal via `TradeManager.close_trade()`.
- **IOC limit entry.** Positions are opened via `ioc_entry` in `bot.py` using an IOC limit order at the current best ask (BUY) or best bid (SELL). Retries on each iteration with a fresh price until fully filled. The only abort condition is price drifting beyond `entry.max_price_deviation_pct` (default 0.3%) from the signal candle close price; on partial fill at abort, stop/TP orders are still placed to protect the live position. Stop losses and trailing TP are anchored to the actual fill price, not the signal price. The crossover candle close price is passed through `TradeSignal.entry_price`; strategies that don't set it fall back to a fresh mark price fetch.
- **Retry with backoff lives in `utils/general.py`** via `with_retry()`. All order and account modules import it from there — nothing else reinvents it.
- **State persistence on crash.** On startup, re-query Binance for open positions rather than trusting a local cache.
- **Config separation.** Secrets in `.env`, everything else (symbols, intervals, risk limits, strategy selection and params) in `config.yaml`.
- **Strategy selection.** `config.yaml` sets `trading.strategy` (key into `STRATEGIES` in `strategies.py`) and `trading.strategy_params`. To add a new strategy, write a function in `strategies.py` and register it in `STRATEGIES` — no other file changes needed.
- **Active strategy: `ema_trend_momentum`.** Five-gate system: (1) fast/slow EMA alignment — no fresh crossover required, aligned EMAs are sufficient so cold-starts and post-close re-entries are handled automatically; (2) 1h 200 EMA trend filter — price must be above (long) or below (short) the trend EMA; (3) RVOL — current candle volume > 1.2× rolling average of previous 20 candles; (4) RSI momentum — RSI 50–70 for longs, 30–50 for shorts; (5) ADX regime filter — ADX ≥ `min_adx` (default 27); skips entry in choppy/ranging markets where EMA crossovers are unreliable. Exits: EMA cross-back in the opposite direction, or RSI ≥ 75 (long) / ≤ 25 (short). ADX does not affect exit logic — only entry is gated. The 1h 200 EMA is derived by resampling the sub-hourly buffer — no second WebSocket stream is needed. `ma_crossover` is retained for reference. **Stagnation exit:** `bot.py` calls `trade_manager.tick_stagnation()` on every HOLD candle. Every `stagnation_candles` (default 4) ticks it evaluates two conditions: (a) **stagnation** — price moved less than `stagnation_min_pct` in the trade's favour AND ADX is below `min_adx` AND RSI has left the entry momentum zone (all three required); (b) **reversal** — price moved *any amount* against the trade from the last checkpoint (`price_pct < 0`), regardless of ADX or RSI. Either triggers a CLOSE. On a passing window the checkpoint resets to the current price so each window measures progress from where the previous one left off, not from entry.
- **WebSocket-driven loop.** On startup the bot pre-fetches a REST candle history per symbol for indicator warmup, then subscribes to a Binance Futures kline WebSocket stream via `ThreadedWebsocketManager` (in `utils/market.py`). The main thread blocks on a `queue.SimpleQueue`; each time a candle closes the WS callback pushes `(symbol, candle)` onto the queue and the main thread runs the strategy. There is no polling sleep. If the last REST candle is still open when the first WS closed candle arrives (same `open_time`), the buffer entry is replaced in place. `ThreadedWebsocketManager` reconnects automatically on disconnect; no candle gap recovery is implemented.
- **Interval-driven candle limit.** The candle prefetch count is auto-computed as `(200 × 60 / interval_minutes) + 50` to always cover 200 complete 1h bars for the trend EMA. If `trading.candle_limit` is set explicitly in `config.yaml` it overrides the auto-computed value. `get_futures_ohlcv` paginates automatically when the required count exceeds Binance's 1500-candle-per-request cap. Strategy params (`fast_period`, `slow_period`, `rsi_period`, `volume_lookback`) are used AS-IS from config — no auto-scaling is applied. When changing `trading.interval`, update these params manually to match. `config.yaml` contains a tuning guide with recommended values per interval. `interval_to_minutes` in `utils/indicators.py` performs the interval string → minutes conversion (used for the candle limit calculation).
- **`TradeManager` owns all open trade state.** `utils/trade_manager.py` runs a background daemon thread polling Binance every `trade_manager.poll_interval_secs` seconds (default 10) per tracked symbol. It replaces the old per-tick reconciliation. Nothing is logged when a poll finds no change. On external close (stop or TP fired): identifies which order triggered by comparing tracked IDs against open orders, cancels only the identified leftover orders (not the one that already fired), verifies cancellation, then queries and logs realized P&L via `account.get_futures_recent_trades` bounded by `registered_at_ms`. On partial TP limit fill: logs fill percentage, re-places stop orders at the reduced size, verifies new orders are live, and logs cumulative P&L. **SL profit-lock milestone:** on every poll where position size is unchanged, `TradeManager` reads `unrealized_pnl` directly from the position response (Binance's authoritative figure — accounts for funding fees). If unrealized profit reaches `risk.sl_profit_trigger_pct` (default 0.6%), it moves both stops to profit-lock levels — stop-limit to `risk.sl_profit_lock_pct` (default 0.2%) above/below entry, stop-market to `risk.sl_profit_market_lock_pct` (default 0.1%) as a safety net. New orders are placed first, old ones cancelled after — no unprotected window. A `sl_moved` flag prevents repeat moves. `execute_signal` calls `trade_manager.register_trade()` on open and `trade_manager.close_trade()` on strategy-driven close (which cancels all four exit orders via `_cancel_all_orders`). Recovered positions (restart) are registered with `has_order_details=False` — external closes are detected but stop quantities are not adjusted on partial fills, and the SL milestone is skipped. **Stagnation state:** `_TradeState` also holds `candle_count` (incremented by `tick_stagnation()` on every candle close) and `checkpoint_price` (initialised to `entry_price`, updated to current price on each passing stagnation window). Both are reset automatically when the state is removed on close.
- **One position at a time.** The bot tracks one `Position` state per symbol (`NONE/LONG/SHORT`). The strategy receives this state on every tick and must return the correct action (`OPEN_LONG`, `OPEN_SHORT`, `CLOSE`, `HOLD`). A new position is only opened when `Position.NONE`. State survives restarts by re-querying Binance on startup.
- **Exit orders on every open.** Immediately after a limit entry fills, `execute_signal` places four exit orders: (1) stop-limit at `risk.stop_loss_limit_pct` (preferred SL, less slippage); (2) stop-market at `risk.stop_loss_market_pct` (safety net); (3) trailing stop at `risk.trailing_take_profit_activation_pct` activation / `risk.trailing_take_profit_callback_rate` callback; (4) maker GTC limit TP at `risk.take_profit_limit_pct` (default 3%). All are `reduceOnly=True`. Order IDs are held by `TradeManager` in memory — on restart they are not recovered, so orders remain live on Binance but the bot will not cancel them on a strategy-driven close. Query open algo orders manually after an unclean restart.
- **Daily P&L reporting.** `DailyPnLReporter` in `utils/pnl_reporter.py` runs as a daemon thread started in `_run()`. It sleeps until 00:00:05 UTC, then calls `account.get_futures_trades_for_range` for each configured symbol over the just-completed UTC day (Binance day boundaries align with UTC midnight). Net P&L (realized_pnl − commission) is written as CSV rows — one per symbol plus a TOTAL row — to `logs/pnl.csv` (configured via `reporting.pnl_csv_file`). The CSV header is written only on first file creation. Symbols that error are skipped in the CSV; errors are logged via loguru. A one-line summary is also emitted to the main loguru log. Fetches up to 1000 trades per symbol per request (Binance cap); sufficient for this bot's trade frequency.
- **Crash notifications via Resend.** `run()` in `bot.py` wraps `_run()` in a try/except. Any unhandled exception calls `general.send_crash_email()`, which sends the exception type, message, and full traceback to `CRASH_NOTIFY_EMAIL` via the Resend API, then re-raises. Per-symbol errors caught in the inner loop do not trigger an email — only bot-killing crashes do. `load_dotenv()` is called inside `_run()` before anything can crash, so env vars are always loaded by the time the crash handler runs.

## Execution boundaries

- Claude must NOT run shell commands. Propose them in chat; the user runs them.
- Claude must NOT run git commands. The user handles all version control.
- Claude may create, edit, and delete files in the project.
- If a task requires running code, Claude outputs the exact command and waits for results.

## Coding conventions

- Type hints on all public functions.
- Docstrings on all public functions in `utils/`.
- No `print()` — always loguru.
- Keep `bot.py` thin: orchestration only, no strategy logic or direct API calls.

## When generating code

- New dependencies go in `requirements.txt` with an explanation.
- Changes touching Docker must update both `Dockerfile` and `docker-compose.yml`.
- Changes touching secrets handling must re-verify `.gitignore` and `.dockerignore` coverage.
- Prefer editing existing files over creating new ones unless a new module clearly belongs.
- Mock `utils/account.py` and `utils/market.py` in unit tests — never hit the real API.
- **After every change, check whether `CLAUDE.md` and `README.md` need updating.** If the file layout, hard rules, design decisions, dependencies, configuration, or usage instructions are affected — update them as part of the same task, not as a follow-up.
