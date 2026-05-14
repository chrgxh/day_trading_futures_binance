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
│   ├── position_store.py        # Persistent JSON store: symbol → owning strategy + state + order IDs
│   ├── risk_guard.py            # Entry gate: max positions, one-per-symbol, daily loss
│   ├── pnl_reporter.py          # Daily P&L CSV + email report (lifecycle owned by StateManager)
│   └── strategies/
│       ├── __init__.py          # STRATEGIES registry
│       ├── base.py              # Strategy ABC — multi-interval buffers, signal computation, execution
│       ├── live_trade_manager.py# Optional per-strategy post-fill lifecycle hooks
│       └── adaptive_trend_pullback.py  # Active strategy
├── utils/
│   ├── general.py               # build_client, with_retry, round_price, send_*_email, order normalizers
│   ├── account.py               # Account state: connection, balances, positions, symbol info, leverage, trades
│   ├── orders.py                # Regular orders: market, limit, tp_limit, get_open_orders, cancel
│   ├── algo_orders.py           # Conditional orders: stop/TP market and limit, cancel_algo
│   ├── positions.py             # Position management: close_position
│   ├── market.py                # Public market data: OHLCV, mark price, multi-(symbol,interval) WS with gap recovery
│   └── indicators.py            # Raw indicators (SMA, EMA, MACD, ADX, ATR, RSI, daily_anchored_vwap, resample_to_1h)
├── config.yaml                  # symbols, strategies list (each declares its own intervals), risk_guard, state_manager, logging
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
│   │   ├── test_position_store.py
│   │   ├── test_risk_guard.py
│   │   ├── test_live_trade_manager.py
│   │   ├── test_strategy_base.py
│   │   └── test_adaptive_trend_pullback.py
│   └── integration/             # Testnet integration tests
│       ├── conftest.py
│       ├── test_account.py
│       ├── test_market.py
│       ├── test_orders.py
│       ├── test_algo_orders.py
│       ├── test_positions.py
│       └── test_notifications.py
├── logs/                        # Mounted volume, not in image
├── state/                       # Mounted volume — positions.json (owning-strategy cache)
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
Loads config, builds the Binance client, fetches symbol info once, builds `StateManager` (which loads `state/positions.json`), builds `RiskGuard`, builds the configured strategies (each `attach_strategy`s itself to StateManager via the base class), prefetches warmup candles per unique `(symbol, interval)`, then calls `state_manager.start()` (sync poll + polling thread) and `strategy.adopt_pre_existing()` for each strategy to rehydrate any positions carried across restart. Finally opens a single WebSocket connection covering every `(symbol, interval)` pair and routes closed candles to matching strategies. No strategy logic, no risk logic, no order placement.

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
- **Persistent ownership store (`state/positions.json`):** atomically rewritten on every poll. After updating `SymbolState`, prunes entries whose position is gone on Binance or whose owner strategy is no longer configured (warning logged for the latter). Strategies use `register_owner` / `update_owner` / `get_owner` to record and recover the strategy-specific state they need across restart. Binance is always the source of truth; the file is a cache.

On `start()`, runs one synchronous poll before returning so callers see accurate state immediately (covers the restart-recovery case).

### PositionStore (`core/position_store.py`)
A thin JSON store keyed by symbol. Schema (versioned for future migrations):

```
{ "version": 1, "updated_at": "<UTC ISO>", "positions": {
    "<SYMBOL>": {
      "strategy": "<name>", "opened_at": "<UTC ISO>",
      "side": "LONG"|"SHORT", "entry_price": "<dec>", "qty": "<dec>",
      "strategy_state": { ... opaque blob owned by strategy ... },
      "orders": { "stop_loss_id": <int>, "tp1_id": <int>, ... }
    }, ...
}}
```

Writes use a write-to-temp-then-rename so crashes never leave a partial file. A corrupt or wrong-version file is quarantined (`<file>.corrupt-<ts>`) and the store starts empty. Lifecycle is owned by `StateManager` — no other module touches it.

### RiskGuard
Stateless gate. `allow_open(symbol, strategy)` returns False if:
- The symbol already has a position on Binance (one-per-symbol, absolute).
- The number of open positions is at `risk_guard.max_concurrent_positions`.
- Cumulative realized daily loss has reached `risk_guard.max_daily_loss_usdt`. When tripped, blocks all new entries for the rest of the UTC day, sends a single warning email via Resend, resumes next UTC day automatically.

### Strategy (ABC)
One instance per strategy entry in config. Each strategy:
- Declares one or more `intervals` (derived from `params` such as `entry_interval` and `regime_interval`) — the bot subscribes to a WebSocket stream for every `(symbol, interval)` pair across all strategies.
- Owns a per-`(symbol, interval)` candle buffer (`self._buffers[symbol][interval]`).
- Implements `compute_signal(symbol, candles) -> Signal | None` — pure decision logic, returns a `Signal` with `entry_price`, `stop_loss_price`, `take_profit_price` for OPEN actions.
- Implements `execute_open(signal)` — owns the entry mechanics (IOC, market, layered limits, whatever) and places its own exit orders via broker primitives.
- Calls `state_manager.mark_change(symbol)` before/after placing orders to suppress orphan warnings during the grace window.
- After a fill, calls `state_manager.register_owner(symbol, ...)` with the strategy-specific state needed to resume management after a restart; updates that entry via `state_manager.update_owner(...)` whenever local state or order IDs change (e.g. extrema, trailing stop replacement).
- Overrides `serialize_state(symbol)` and `adopt(symbol, entry)` if it needs restart recovery. The base `adopt_pre_existing()` walks symbols, looks up the owner entry, and calls `adopt` for entries whose `strategy` matches `self.name`.
- Optionally owns a `LiveTradeManager` (per-strategy lifecycle hooks tied to StateManager poll cadence). Strategies whose lifecycle decisions are tied to closed candles (not poll cadence) skip the LTM and manage exits directly in `_tick`.

`on_candle(symbol, interval, candle)` is the only entry point the bot calls. The default `_tick(symbol, interval)` updates the buffer, checks `state_manager.has_position(symbol)`, runs `compute_signal`, and dispatches to `risk_guard.allow_open` / `execute_open`. Multi-interval strategies override `_tick` to coordinate across intervals (e.g. higher-TF regime filter + lower-TF execution).

### LiveTradeManager (optional, per-strategy)
Base class with three override points: `on_open(symbol)`, `on_update(state)`, `on_close(symbol)`. Subscribes to `StateManager` updates. The base class has no behavior — concrete subclasses implement strategy-specific lifecycle logic (e.g. SL migration, partial-fill re-stop, stagnation exits). Configured per strategy in `config.yaml`; absent if a strategy doesn't need it.

### Active strategy: `adaptive_trend_pullback`
Multi-timeframe trend-pullback system. Both intervals (`entry_interval`, `regime_interval`) and every indicator period are configurable in `params`.

- **Regime filter (regime_interval, e.g. 4h):** longs require `close > EMA_slow`, `EMA_fast > EMA_slow`, and positive `EMA_fast` slope over `regime_slope_lookback` bars. Shorts inverse.
- **Entry gates (entry_interval, e.g. 30m, longs; shorts inverse):** pullback (at least one of the last `pullback_lookback` prior bar lows within `pullback_proximity_pct` of `EMA_fast` or daily-anchored VWAP), close > prev close, bullish close, volume > volume_SMA, ADX > `adx_min`, ATR > SMA(ATR), RSI < `rsi_max_long`, close > `EMA_fast`, close > pullback high. Daily-anchored VWAP resets at UTC 00:00 and is toggled via `vwap_enabled`.
- **SL/TP per signal:** stop = close − `stop_atr_mult` × ATR; TP1 = close + `tp1_r_multiple` × R, sized to `tp1_size_pct` of filled qty, placed as GTX post-only LIMIT reduce-only (retries `tp1_retry_attempts` times on rejection, then accepts no-TP1 with a warning).
- **Entry execution:** IOC limit chasing best ask/bid, re-quoting every `ioc_poll_secs` (default 3s — rate-limit-safe) until filled, drifted past `max_price_deviation_pct`, or `entry_timeout_secs` elapsed.
- **Exits managed inside the strategy on every closed entry-interval candle** (no LiveTradeManager). Three checks per candle, in order:
  1. **Trend invalidation** (exit on any): close beyond `invalidation_structure_lookback`-bar low/high (structure break); `EMA_fast` slope flip; close beyond `EMA_fast` by ≥ `invalidation_strong_close_atr_mult` × ATR against the position; ADX drop > `invalidation_momentum_adx_drop` over last `invalidation_momentum_lookback` bars AND ADX < `invalidation_momentum_adx_floor` (momentum collapse).
  2. **Dead-trade exit** (only after `candles_since_entry > dead_trade_min_candles`, exit on all): ADX < ADX `dead_trade_adx_lookback` bars ago AND ADX < `dead_trade_adx_floor`; ATR < SMA(ATR, `atr_sma_period`); unrealized PnL per unit < `dead_trade_r_floor` × R. Rechecked every closed entry-interval candle.
  3. **Trailing stop update:** trail = `highest_close_since_entry` − `trail_atr_mult` × ATR (inverted for shorts). Only moved when more favorable. Place-then-cancel ordering (new stop placed first, then old one cancelled) so the position is never momentarily unprotected.
- **Position sizing:** `qty = notional_per_trade_usdt / entry_price`, rounded down to `step_size`. `leverage` is set per-symbol at startup from `params.leverage`.
- **Restart recovery:** positions opened by this strategy are persisted to `state/positions.json` via StateManager. `serialize_state` writes `entry_atr`, `r_distance`, `entry_candle_open_time`, and the running `highest_close` / `lowest_close`; `adopt` rehydrates `_ManagedPosition` and reconciles saved order IDs against live Binance orders. If the saved stop-loss is missing on adopt (cancelled or filled during downtime), a fresh STOP_MARKET is placed at `entry_price ± r_distance` (warning logged). Missing TP1 is not re-created.

### Multi-strategy on the same symbol
Symbol ownership is absolute and short-lived. The first strategy in `config.yaml.strategies` that fires on a given symbol opens the position; every other strategy sees `state_manager.has_position(symbol) == True` and stays silent until the position closes. Strategies do not coordinate directly — they coordinate through StateManager.

### WebSockets
One `_KlineStreamManager` connection covers every `(symbol, interval)` pair. The bot opens streams for `pairs = unique_intervals × symbols`. Gap recovery on every closed candle: if `new.open_time - last.open_time > interval_ms`, REST-fetches the missing range via `get_futures_ohlcv` and delivers the back-filled candles before the new one, logging a `[ws] gap-fill` warning. Reconnects automatically on disconnect.

### Restart recovery
Startup order in `bot.py`: build StateManager (loads `state/positions.json`) → build strategies (each calls `state_manager.attach_strategy` in the base `__init__`) → warmup → `state_manager.start()` (sync poll populates `_states`, prunes file entries whose Binance position is gone or whose strategy is no longer configured) → `strategy.adopt_pre_existing()` per strategy (rehydrates internal state and reconciles order IDs against live Binance orders).

If the file shows a position with a strategy that's no longer in `config.yaml`, the entry is dropped and a warning is logged; the position itself is left untouched on Binance ("untracked-position" — see StateManager). Positions that exist on Binance but have no entry in the file are treated the same way (untracked) — adoption is opt-in by file presence.

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
