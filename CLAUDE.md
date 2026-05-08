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
│   ├── general.py           # Shared primitives — build_client, with_retry, send_crash_email, order normalizers
│   ├── account.py           # Account state layer — connection, balances, positions, symbol info, leverage
│   ├── orders.py            # Regular orders — market, limit, get_open_orders, cancel, cancel_all
│   ├── algo_orders.py       # Conditional orders — stop/TP market and limit variants, cancel_algo
│   ├── positions.py         # Position management — close_position
│   ├── market.py            # Public market data (OHLCV, mark price)
│   └── indicators.py        # Signal types (Signal, TradeSignal) and raw indicators (SMA, EMA, MACD, ADX)
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
│   └── integration/         # Testnet integration tests — run with: pytest -m integration
│       ├── conftest.py      # Loads .env.testnet, client/sym_info/open_position fixtures
│       ├── test_account.py
│       ├── test_market.py
│       ├── test_orders.py
│       ├── test_algo_orders.py
│       ├── test_positions.py
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
- **Risk controls live in `bot.py`** — max position size, max daily loss, kill switch. Strategies decide intent; `bot.py` enforces limits before anything reaches `utils/account.py`. On every position open, `bot.py` places dual stop losses (stop-limit + stop-market via the algo API) and a trailing take profit (`TRAILING_STOP_MARKET` via the regular order API). All three are cancelled explicitly on a CLOSE signal.
- **GTX (post-only) limit entry.** Positions are opened with a `timeInForce="GTX"` limit order at the current mark price, not a market order. GTX (also called Post-Only) guarantees the order is a maker; if it would immediately fill as a taker, Binance rejects it and the bot retries. On each attempt `bot.py` fetches the current mark price, checks that it hasn't drifted more than `entry.max_price_deviation_pct` (default 0.3%) from the crossover candle close price, and places a new limit. The order is polled every 2 seconds for up to `entry.limit_order_timeout_secs` (default 20s). If it hasn't filled, it's cancelled and the bot retries, up to `entry.max_retries` (default 3) attempts. If price drifts too far at any point, the entry is aborted. Stop losses and trailing TP are anchored to the actual fill price, not the signal price. The crossover candle close price is passed through `TradeSignal.entry_price`; strategies that don't set it fall back to a fresh mark price fetch.
- **Retry with backoff lives in `utils/general.py`** via `with_retry()`. All order and account modules import it from there — nothing else reinvents it.
- **State persistence on crash.** On startup, re-query Binance for open positions rather than trusting a local cache.
- **Config separation.** Secrets in `.env`, everything else (symbols, intervals, risk limits, strategy selection and params) in `config.yaml`.
- **Strategy selection.** `config.yaml` sets `trading.strategy` (key into `STRATEGIES` in `strategies.py`) and `trading.strategy_params`. To add a new strategy, write a function in `strategies.py` and register it in `STRATEGIES` — no other file changes needed.
- **WebSocket-driven loop.** On startup the bot pre-fetches 200 REST candles per symbol for indicator warmup, then subscribes to a Binance Futures kline WebSocket stream via `ThreadedWebsocketManager` (in `utils/market.py`). The main thread blocks on a `queue.SimpleQueue`; each time a candle closes the WS callback pushes `(symbol, candle)` onto the queue and the main thread runs the strategy. There is no polling sleep. If the last REST candle is still open when the first WS closed candle arrives (same `open_time`), the buffer entry is replaced in place. `ThreadedWebsocketManager` reconnects automatically on disconnect; no candle gap recovery is implemented.
- **One position at a time.** The bot tracks one `Position` state per symbol (`NONE/LONG/SHORT`). The strategy receives this state on every tick and must return the correct action (`OPEN_LONG`, `OPEN_SHORT`, `CLOSE`, `HOLD`). A new position is only opened when `Position.NONE`. State survives restarts by re-querying Binance on startup.
- **Dual stop losses on every open.** Immediately after placing a market order, `execute_signal` places two conditional orders: a stop-limit at `risk.stop_loss_limit_pct` (preferred exit, less slippage) and a stop-market at `risk.stop_loss_market_pct` (safety net if price gaps past the limit). Both use `reduceOnly=True`. When the strategy signals `CLOSE`, both are cancelled before `close_position` is called. Stop order IDs are held in memory only — on restart they are not recovered, so the orders remain live on Binance but the bot will not cancel them on a strategy-driven close. Query open algo orders manually after an unclean restart.
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
