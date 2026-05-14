# Binance Futures Trading Bot

Automated futures trading bot for Binance. Multiple strategies run in parallel at independent intervals over a shared list of symbols. Each strategy owns its own entry mechanics, SL/TP pricing, and optional post-fill lifecycle hooks. The bot itself is a thin orchestrator — startup wiring and WebSocket routing only.

## Structure

```
bot.py                              — entry point: load config, wire objects, route candles
core/
  types.py                          — Position, Action, Signal, SymbolState
  state_manager.py                  — single Binance poller, source of truth for live state
  risk_guard.py                     — entry gate: max positions, one-per-symbol, daily loss
  pnl_reporter.py                   — daily P&L CSV + email (lifecycle owned by StateManager)
  strategies/
    base.py                         — Strategy ABC: candle buffer, signal, execution
    live_trade_manager.py           — optional per-strategy post-fill lifecycle hooks
    ema_trend_momentum.py           — active strategy
utils/
  general.py                        — build_client, with_retry, round_price, emails, normalizers
  account.py                        — connection, balances, positions, symbol info, trades
  orders.py                         — regular orders + get_open_orders
  algo_orders.py                    — conditional orders (stop/TP market & limit)
  positions.py                      — close_position
  market.py                         — OHLCV, mark price, multi-(symbol,interval) WS with gap-fill
  indicators.py                     — SMA, EMA, MACD, ADX, RSI, resample_to_1h, interval_to_minutes
config.yaml                         — symbols, leverage, strategies list, risk_guard, state_manager, logging
.env                                — mainnet API keys (never commit)
.env.testnet                        — testnet API keys for integration tests (never commit)
tests/
  unit/                             — fast unit tests (no network)
  integration/                      — testnet integration tests
logs/                               — runtime logs (mounted volume): bot.log, pnl.csv, bot.debug.log
sandbox.ipynb                       — manual testnet notebook
```

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=true                # set to false for mainnet

# optional — crash + daily report emails via Resend
RESEND_API_KEY=
CRASH_NOTIFY_EMAIL=
CRASH_NOTIFY_FROM_EMAIL=            # must be a verified sender domain in Resend
```

For integration tests, copy `.env.testnet.example` to `.env.testnet` and fill in your testnet credentials (plus the Resend vars if you want to run `test_notifications`).

### config.yaml

Single source of truth for everything non-secret. Shape:

```yaml
symbols: [BTCUSDT, ETHUSDT, SOLUSDT]   # every strategy watches every symbol
leverage: 20

strategies:                            # ordered; first listed wins on a symbol collision
  - name: ema_trend_momentum
    interval: 15m
    params: {...}                      # shared across all symbols for this strategy
    live_trade_manager:
      enabled: false
      params: {}

risk_guard:
  max_concurrent_positions: 3
  max_daily_loss_usdt: 50.0

state_manager:
  poll_interval_secs: 10
  grace_period_secs: 15
  pnl_refresh_every_n_polls: 6
  pnl_reporter:
    enabled: true
    csv_file: logs/pnl.csv

logging:
  level: INFO
  log_file: logs/bot.log
  rotation: "10 MB"
  retention: "7 days"
  debug_log_file: logs/bot.debug.log
```

### Adding a strategy

1. Add a class in `core/strategies/your_strategy.py` extending `Strategy`. Implement `compute_signal(symbol, candles)` and `execute_open(signal)`.
2. Register it in `core/strategies/__init__.py`'s `STRATEGIES` dict.
3. Add an entry under `strategies:` in `config.yaml` with its `name`, `interval`, and `params`.

Strategies decide:
- What action to take on each candle (`compute_signal`).
- What `entry_price`, `stop_loss_price`, and `take_profit_price` to aim for.
- How to actually enter the position (`execute_open` — IOC, market, layered limits, whatever).
- Whether they want a `LiveTradeManager` for post-fill lifecycle (SL migration, partial-fill handling, stagnation, etc.) — subclass `LiveTradeManager` and override `on_open` / `on_update` / `on_close`.

## Running

**Locally:**
```bash
python bot.py
```

**Docker:**
```bash
docker compose up --build
```

## Architecture in one paragraph

`StateManager` polls Binance every few seconds and is the single source of truth for live state — positions, orders, daily P&L. It cancels orphan orders and warns on untracked positions; it never tries to manage positions itself. `RiskGuard` is a stateless gate that reads StateManager and enforces max-positions / one-per-symbol / daily-loss limits. Strategies receive closed candles via WebSocket, ask StateManager whether their symbol is free, compute a signal, and (for OPEN actions) consult RiskGuard before placing orders themselves. Each strategy can optionally attach a `LiveTradeManager` that subscribes to StateManager updates for post-fill lifecycle. The bot itself only does wiring and WebSocket routing.

## Daily P&L report

At 00:00:05 UTC each day the bot:

1. Appends rows to `logs/pnl.csv` for the just-completed UTC day — one row per symbol plus a TOTAL row.
2. Sends an email to `CRASH_NOTIFY_EMAIL` with the P&L table and every WARNING / ERROR / CRITICAL log line from that day.

The email is silently skipped if Resend env vars are absent. The CSV path is configurable via `state_manager.pnl_reporter.csv_file`.

> **Note:** the report only fires if the bot is alive at midnight. A bot stopped before midnight and restarted after skips that day's report.

## Testing

Unit tests run without any network connection:

```bash
pytest tests/unit/
```

Integration tests run against the live Binance testnet and require `.env.testnet`:

```bash
pytest -m integration
```

Plain `pytest` (no `-m integration`) runs unit tests only and skips integration tests.

## Safety defaults

- `BINANCE_TESTNET=true` — connects to testnet by default.
- `risk_guard.max_concurrent_positions` — caps how many symbols can be live at once.
- One-position-per-symbol is absolute (enforced by RiskGuard, regardless of strategy).
- `risk_guard.max_daily_loss_usdt` — once tripped, blocks new entries for the rest of the UTC day; existing positions run their course; a single warning email is sent; bot resumes next UTC day automatically.
- `StateManager` cancels orphan orders (orders with no matching position) and warns on untracked positions (position with no exit orders), with a configurable grace window so freshly placed orders are not misclassified.
- WebSocket gap recovery — on every candle close, missing candles after a reconnect are REST-fetched and back-filled before being delivered to strategies; a `[ws] gap-fill` warning is logged.
- Per-strategy exit orders: the active `ema_trend_momentum` strategy places four reduceOnly exits on every position open (stop-limit, stop-market, trailing TP, GTC limit TP), all anchored to the actual fill price. Binance auto-cancels them when the position hits zero.
