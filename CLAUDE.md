# Day Trading Bot

Context file for Claude Code. Read this before suggesting changes or generating code.

## Project goal

A futures day trading bot for Binance Futures. Multiple strategies can run in parallel at different intervals over a shared list of symbols; each strategy decides its own entry mechanics and SL/TP pricing. The bot itself is a thin orchestrator.

## Tech stack

- **Language:** Python 3.11+
- **Exchange API:** Binance Futures (via the official `python-binance` client + direct WS via `websockets`)
- **Logging:** loguru
- **Notifications:** Resend (crash + daily report emails via `resend` SDK)
- **Runtime:** Docker (single container, `docker compose` for local dev)
- **Config:** `.env` for secrets, `config.yaml` for everything else

## File layout

```
day-trading-bot/
├── bot.py                       # Thin entry point — startup wiring + WS routing
├── core/
│   ├── __init__.py
│   ├── types.py                 # Position, Action, Signal, SymbolState dataclasses
│   ├── state_manager.py         # Single Binance poller, source of truth for live state
│   ├── risk_guard.py            # Entry gate: max positions, one-per-symbol, daily loss
│   ├── pnl_reporter.py          # Daily P&L CSV + email report (lifecycle owned by StateManager)
│   └── strategies/
│       ├── __init__.py          # STRATEGIES registry
│       ├── base.py              # Strategy ABC — owns candle buffer, signal computation, execution
│       ├── live_trade_manager.py# Optional per-strategy post-fill lifecycle hooks
│       └── ema_trend_momentum.py# Active strategy
├── utils/
│   ├── general.py               # build_client, with_retry, round_price, send_*_email, order normalizers
│   ├── account.py               # Account state: connection, balances, positions, symbol info, leverage, trades
│   ├── orders.py                # Regular orders: market, limit, tp_limit, get_open_orders, cancel
│   ├── algo_orders.py           # Conditional orders: stop/TP market and limit, cancel_algo
│   ├── positions.py             # Position management: close_position
│   ├── market.py                # Public market data: OHLCV, mark price, multi-(symbol,interval) WS with gap recovery
│   └── indicators.py            # Raw indicators (SMA, EMA, MACD, ADX, RSI, resample_to_1h, interval_to_minutes)
├── config.yaml                  # symbols, leverage, strategies list, risk_guard, state_manager, logging
├── .env                         # Secrets ONLY — never committed
├── .env.example
├── .env.testnet                 # Testnet secrets for integration tests — never committed
├── .env.testnet.example
├── .gitignore
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pytest.ini
├── tests/
│   ├── unit/                    # Fast unit tests — no network
│   │   ├── test_indicators.py
│   │   ├── test_general.py
│   │   ├── test_state_manager.py
│   │   ├── test_risk_guard.py
│   │   ├── test_live_trade_manager.py
│   │   ├── test_strategy_base.py
│   │   └── test_ema_trend_momentum.py
│   └── integration/             # Testnet integration tests
│       ├── conftest.py
│       ├── test_account.py
│       ├── test_market.py
│       ├── test_orders.py
│       ├── test_algo_orders.py
│       ├── test_positions.py
│       └── test_notifications.py
├── logs/                        # Mounted volume, not in image
└── sandbox.ipynb                # Manual testnet notebook
```

## Hard rules

1. **Secrets never leave `.env`.** `.env` must stay in `.gitignore` and `.dockerignore`.
2. **All Binance API calls go through `utils/` modules.** `bot.py`, `core/`, and strategies must not import `binance` directly except via the broker primitives in `utils/`.
3. **Futures only.** Use `futures_*` methods exclusively.
4. **Every action is logged via loguru** to stdout + the file in `logs/` (mounted volume).
5. **The log file is created at runtime in the mounted volume**, not baked into the image.

## Architecture

### Bot.py (thin orchestrator)
Loads config, builds the Binance client, fetches symbol info once, builds `StateManager`, builds `RiskGuard`, builds the configured strategies, prefetches warmup candles per unique `(symbol, interval)`, opens a single WebSocket connection covering every `(symbol, interval)` pair, and routes closed candles to matching strategies. No strategy logic, no risk logic, no order placement.

### StateManager
Single Binance poller. On every `state_manager.poll_interval_secs`:
- Fetches all positions (one call) and open orders per configured symbol.
- Builds a `SymbolState` per symbol (position side/size/entry/mark/unrealized P&L + all open orders).
- **Orphan reconciliation:** orders with no matching position are cancelled and a warning is logged.
- **Untracked-position warning:** a position with no exit orders is logged as a warning and left alone — StateManager never manages positions, only observes.
- Notifies subscribers (every `LiveTradeManager` is subscribed for its strategy's symbols).
- A grace period (`state_manager.grace_period_secs`) suppresses orphan/untracked warnings briefly after a strategy calls `state_manager.mark_change(symbol)` (placed/cancelled orders).
- Every `state_manager.pnl_refresh_every_n_polls`, refreshes daily net P&L and trade count by querying `account.get_futures_recent_trades` per symbol for the current UTC day.
- Daily P&L resets at UTC midnight.
- Optionally drives a `DailyPnLReporter` (CSV + email) at UTC midnight.

On `start()`, runs one synchronous poll before returning so callers see accurate state immediately (covers the restart-recovery case).

### RiskGuard
Stateless gate. `allow_open(symbol, strategy)` returns False if:
- The symbol already has a position on Binance (one-per-symbol, absolute).
- The number of open positions is at `risk_guard.max_concurrent_positions`.
- Cumulative realized daily loss has reached `risk_guard.max_daily_loss_usdt`. When tripped, blocks all new entries for the rest of the UTC day, sends a single warning email via Resend, resumes next UTC day automatically.

### Strategy (ABC)
One instance per strategy entry in config. Each strategy:
- Owns a per-symbol candle buffer (private state).
- Implements `compute_signal(symbol, candles) -> Signal | None` — pure decision logic, returns a `Signal` with `entry_price`, `stop_loss_price`, `take_profit_price` for OPEN actions.
- Implements `execute_open(signal)` — owns the entry mechanics (IOC, market, layered limits, whatever) and places its own exit orders via broker primitives.
- Calls `state_manager.mark_change(symbol)` before/after placing orders to suppress orphan warnings during the grace window.
- Optionally owns a `LiveTradeManager` (per-strategy lifecycle hooks).

`on_candle(symbol, candle)` is the only entry point the bot calls. It updates the buffer, checks `state_manager.has_position(symbol)` (skip if any position exists — first strategy in config wins), runs `compute_signal`, and if it's an OPEN action calls `risk_guard.allow_open`, then `execute_open`.

### LiveTradeManager (optional, per-strategy)
Base class with three override points: `on_open(symbol)`, `on_update(state)`, `on_close(symbol)`. Subscribes to `StateManager` updates. The base class has no behavior — concrete subclasses implement strategy-specific lifecycle logic (e.g. SL migration, partial-fill re-stop, stagnation exits). Configured per strategy in `config.yaml`; absent if a strategy doesn't need it.

### Active strategy: `ema_trend_momentum`
Five-gate entry: (1) fast/slow EMA alignment, (2) price above/below 1h 200 EMA (resampled from the strategy's sub-hourly buffer), (3) RVOL spike, (4) RSI band, (5) ADX ≥ `min_adx`. Computes `entry_price` (candle close) and SL/TP prices from percentages in `params` — the per-strategy SL/TP calculation will be reworked later. Execution is IOC limit, chasing best ask/bid until filled or price drifts beyond `max_price_deviation_pct`. After fill places four reduceOnly exits (stop-limit, stop-market, trailing TP, GTC limit TP) anchored to the actual fill price.

### Multi-strategy on the same symbol
Symbol ownership is absolute and short-lived. The first strategy in `config.yaml.strategies` that fires on a given symbol opens the position; every other strategy sees `state_manager.has_position(symbol) == True` and stays silent until the position closes. Strategies do not coordinate directly — they coordinate through StateManager.

### WebSockets
One `_KlineStreamManager` connection covers every `(symbol, interval)` pair. The bot opens streams for `pairs = unique_intervals × symbols`. Gap recovery on every closed candle: if `new.open_time - last.open_time > interval_ms`, REST-fetches the missing range via `get_futures_ohlcv` and delivers the back-filled candles before the new one, logging a `[ws] gap-fill` warning. Reconnects automatically on disconnect.

### Restart recovery
StateManager's first synchronous poll discovers any positions already open on Binance. They are recorded in `SymbolState` and block entries on those symbols (one-per-symbol). No strategy claims them and no `LiveTradeManager` manages them — they run their course on their existing exit orders. The grace period prevents false orphan-cancellation during the first poll.

### Crash notifications
`bot.run()` wraps `_run()`. Any unhandled exception triggers `general.send_crash_email()` with the exception type, message, and traceback, then re-raises. Per-tick errors inside a strategy are caught and logged — they do not crash the bot.

### Daily P&L reporting
`DailyPnLReporter` is owned by `StateManager` (lifecycle), runs as a daemon thread. At 00:00:05 UTC it writes per-symbol + TOTAL rows to `logs/pnl.csv` and emails an HTML report via Resend that includes the day's WARNING/ERROR/CRITICAL log lines.

## Execution boundaries

- Claude must NOT run shell commands. Propose them in chat; the user runs them. (Exception: writing and running Python unit tests is allowed when relevant.)
- Claude must NOT run git commands. The user handles all version control.
- Claude may create, edit, and delete files in the project.
- If a task requires running code beyond unit tests, Claude outputs the exact command and waits for results.

## Coding conventions

- Type hints on all public functions.
- Docstrings on all public functions in `utils/` and `core/`.
- No `print()` — always loguru.
- Keep `bot.py` thin: orchestration only, no strategy logic or direct API calls.
- Log prefixes: `[state]`, `[risk]`, `[ws]`, `[strategy_name]`, `[strategy_name:ltm]`.

## When generating code

- New dependencies go in `requirements.txt` with an explanation.
- Changes touching Docker must update both `Dockerfile` and `docker-compose.yml`.
- Changes touching secrets handling must re-verify `.gitignore` and `.dockerignore` coverage.
- Prefer editing existing files over creating new ones unless a new module clearly belongs.
- Mock `utils/account.py` and `utils/market.py` in unit tests — never hit the real API.
- **After every change, check whether `CLAUDE.md` and `README.md` need updating.**
