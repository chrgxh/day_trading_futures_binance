# Binance Futures Trading Bot

Automated futures trading bot for Binance. Multiple strategies run in parallel at independent intervals over a shared list of symbols. Each strategy owns its own entry mechanics, SL/TP pricing, and optional post-fill lifecycle hooks. The bot itself is a thin orchestrator — startup wiring and WebSocket routing only.

## Structure

```
bot.py                              — entry point: load config, wire objects, route candles
core/
  types.py                          — Position, Action, Signal, SymbolState
  state_manager.py                  — WebSocket-driven source of truth for live state
  risk_guard.py                     — entry gate: max positions, one-per-symbol, daily loss
  pnl_reporter.py                   — daily P&L CSV + email (lifecycle owned by StateManager)
  strategies/
    base.py                         — Strategy ABC: candle buffer, signal, execution
    live_trade_manager.py           — optional per-strategy post-fill lifecycle hooks
    adaptive_trend_pullback.py      — trend-pullback strategy
    bb_rsi_mean_reversion.py        — Bollinger-Band + RSI mean-reversion strategy
    trend_pullback_limit.py         — trend-pullback strategy with a resting limit entry
utils/
  general.py                        — build_client, with_retry, round_price, emails, normalizers
  account.py                        — connection, balances, positions, symbol info, trades
  orders.py                         — regular orders + get_open_orders
  algo_orders.py                    — conditional orders (stop/TP market & limit)
  positions.py                      — close_position
  market.py                         — OHLCV, mark price, multi-(symbol,interval) WS with gap-fill
  user_stream.py                    — authenticated user-data WS (SDK ThreadedWebsocketManager): event delivery
  indicators.py                     — SMA, EMA, MACD, ADX, ATR, RSI, bollinger_bands, daily_anchored_vwap, resample_to_1h
config.yaml                         — symbols, strategies list (each declares its own intervals), risk_guard, state_manager, logging
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

strategies:                            # ordered; first listed wins on a symbol collision
  - name: adaptive_trend_pullback
    params:                            # shared across all symbols for this strategy
      entry_interval: 30m              # strategy declares its own intervals here
      regime_interval: 4h
      leverage: 5
      notional_per_trade_usdt: 100.0
      # ...indicator periods, exit thresholds, etc.
    # live_trade_manager:              # optional, omit if the strategy manages exits itself
    #   enabled: true
    #   params: {...}

risk_guard:
  max_concurrent_positions: 3
  max_daily_loss_usdt: 50.0

state_manager:
  resync_interval_secs: 90   # safety-net full REST resync period; state is WS-driven
  grace_period_secs: 15
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

1. Add a class in `core/strategies/your_strategy.py` extending `Strategy`. Implement `compute_signal(symbol, candles)` and `execute_open(signal)`. In `__init__`, pass `intervals=[...]` to `super().__init__` — usually derived from `params` so config drives the timeframes (e.g. `params["entry_interval"]`, `params["regime_interval"]`).
2. Register it in `core/strategies/__init__.py`'s `STRATEGIES` dict.
3. Add an entry under `strategies:` in `config.yaml` with its `name` and `params` block. The strategy declares its own intervals via params — there is no top-level `interval` field.

Strategies decide:
- Which intervals they need (`self.intervals`) — the bot subscribes to one WebSocket stream per unique `(symbol, interval)` pair across all strategies.
- What action to take on each closed candle (`_tick(symbol, interval)` — override for multi-interval routing, or rely on the default which calls `compute_signal`).
- What `entry_price`, `stop_loss_price`, and `take_profit_price` to aim for.
- How to actually enter the position (`execute_open` — IOC, market, layered limits, whatever).
- Whether they want a `LiveTradeManager` for post-fill lifecycle fired on each StateManager refresh (SL migration, partial-fill handling, stagnation) — subclass `LiveTradeManager` and override `on_open` / `on_update` / `on_close`. Strategies whose exit logic is tied to closed candles should skip the LTM and manage exits directly in `_tick`.

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

`StateManager` is the single source of truth for live state — positions, orders, daily P&L. It is driven by the authenticated Binance user-data WebSocket: account/order events trigger a targeted REST refresh of the affected symbol, and a low-frequency full REST resync runs as a safety net (and after every reconnect) to correct any event drift. It cancels orphan orders and warns on untracked positions; it never tries to manage positions itself. It also owns a small JSON cache at `state/positions.json` mapping each live position to the strategy that opened it plus that strategy's saved state and exit order IDs; the file is rewritten atomically on every state refresh and used by strategies to resume managing positions across restarts. `RiskGuard` is a stateless gate that reads StateManager and enforces max-positions / one-per-symbol / daily-loss limits. Strategies receive closed candles via WebSocket, ask StateManager whether their symbol is free, compute a signal, and (for OPEN actions) consult RiskGuard before placing orders themselves. Each strategy can optionally attach a `LiveTradeManager` that subscribes to StateManager updates for post-fill lifecycle. The bot itself only does wiring and WebSocket routing.

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
- Per-strategy exit orders:
  - `adaptive_trend_pullback` places three reduceOnly exits per entry: a **layered stop** (stop-limit at the ATR stop + a stop-market backstop 0.1% further out — both legs anchored to the actual fill price) plus a GTX post-only TP1 limit at 1.5R for 40% of size. The runner's trailing stop is recomputed on every closed entry-interval candle and the full pair is replaced (place-new → cancel-old) so the position is never momentarily unprotected. Trend-invalidation and dead-trade exits fire from inside the strategy on closed candles.
  - `bb_rsi_mean_reversion` (range-only, macro-aligned) first checks a daily macro bias (1D EMA50/EMA200): UP → longs only, DOWN → shorts only, NEUTRAL → no entry. Then requires the 4h regime to be range-bound (ADX ≤ 20, flat EMAs). On entry it picks the more conservative of an ATR stop and a structure stop (swing low/high) and places a **layered stop** there (stop-limit + stop-market backstop), plus up to two reduceOnly GTC LIMIT take-profits — TP1 at the Bollinger middle band (default 70% of size), TP2 at the opposite band (default 30%). When TP1 partial-fills, the SL is moved to break-even on the next closed candle by replacing the layered pair (place-new → cancel-old, never unprotected) so the runner can't turn a partial winner back into a loser. Entry is a single-shot IOC LIMIT at the signal close: fills at that price or better, otherwise skips (no chasing, no retry, no resting order). Pays the taker fee on entry by design; exits are maker-priced. Invalidation and time-stop exits fire on closed entry-interval candles.
  - `trend_pullback_limit` is the least restrictive of the three — instead of waiting for a confirming candle close, it rests a passive **GTC LIMIT** maker order at the entry-interval `EMA_fast` pullback level whenever a loose 4h EMA-stack trend is in force. The resting order is registered as a *pending entry* (exempt from orphan cancellation; `StateManager` fires `on_fill` / `on_cancel`) and is cancelled if the regime flips or it goes unfilled past `entry_expiry_candles`. On fill, exits are placed off the authoritative fill price: a **layered stop** (stop-limit + stop-market backstop) at `fill ∓ stop_atr_mult × ATR`, plus two reduceOnly GTC LIMIT take-profits at fixed R-multiples (TP1 1R / 50%, TP2 2.5R / 50%). When a TP partial-fills, the SL moves to break-even (layered-pair replace, never unprotected). No trailing stop, no dead-trade gauntlet — the stop + two TPs fully define each trade.
