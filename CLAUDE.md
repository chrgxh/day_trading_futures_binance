# Day Trading Bot

Context file for Claude Code. Read this before suggesting changes or generating code.

## Project goal

A futures day trading bot for Binance Futures. Multiple strategies can run in parallel at different intervals over a shared list of symbols; each strategy decides its own entry mechanics and SL/TP pricing. The bot itself is a thin orchestrator.

## Tech stack

- **Language:** Python 3.11+
- **Exchange API:** Binance Futures (via the official `python-binance` client; WebSockets via the SDK's `ThreadedWebsocketManager`)
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
│   ├── warmup_cache.py          # Disk cache for warmup candles — restart re-fetches only the gap
│   ├── risk_guard.py            # Entry gate: max positions, one-per-symbol, daily loss
│   ├── pnl_reporter.py          # Daily P&L CSV + email report (lifecycle owned by StateManager)
│   └── strategies/
│       ├── __init__.py          # STRATEGIES registry
│       ├── base.py              # Strategy ABC — multi-interval buffers, signal computation, execution
│       ├── live_trade_manager.py# Optional per-strategy post-fill lifecycle hooks
│       ├── adaptive_trend_pullback.py  # Trend-pullback strategy
│       ├── bb_rsi_mean_reversion.py    # Bollinger-Band + RSI mean-reversion strategy
│       └── trend_pullback_limit.py     # Trend-pullback strategy with a resting limit entry
├── utils/
│   ├── general.py               # build_client, with_retry, round_price, send_*_email, order normalizers
│   ├── account.py               # Account state: connection, balances, positions, symbol info, leverage, trades
│   ├── orders.py                # Regular orders: market, limit, tp_limit, get_open_orders, cancel
│   ├── algo_orders.py           # Conditional orders: stop/TP market and limit, cancel_algo
│   ├── positions.py             # Position management: close_position
│   ├── market.py                # Public market data: OHLCV, mark price, multi-(symbol,interval) WS with gap recovery
│   ├── user_stream.py           # Authenticated user-data WS (SDK ThreadedWebsocketManager): account/order event delivery
│   └── indicators.py            # Raw indicators (SMA, EMA, MACD, ADX, ATR, RSI, bollinger_bands, daily_anchored_vwap, resample_to_1h)
├── config.yaml                  # symbols, strategies list (each declares its own intervals), risk_guard, state_manager, warmup_cache, logging
├── scripts/
│   └── prefetch_warmup.py        # Standalone warmup-cache prefetch (no bot lifecycle)
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
│   │   ├── test_market.py
│   │   ├── test_state_manager.py
│   │   ├── test_position_store.py
│   │   ├── test_risk_guard.py
│   │   ├── test_live_trade_manager.py
│   │   ├── test_strategy_base.py
│   │   ├── test_adaptive_trend_pullback.py
│   │   ├── test_bb_rsi_mean_reversion.py
│   │   └── test_trend_pullback_limit.py
│   └── integration/             # Testnet integration tests
│       ├── conftest.py
│       ├── test_account.py
│       ├── test_market.py
│       ├── test_orders.py
│       ├── test_algo_orders.py
│       ├── test_positions.py
│       └── test_notifications.py
├── logs/                        # Mounted volume, not in image
├── state/                       # Mounted volume — positions.json (owning-strategy cache) + warmup_cache.json (candle cache)
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
Loads config, builds the Binance client, fetches symbol info once, builds `StateManager` (which loads `state/positions.json`), builds `RiskGuard`, builds the configured strategies (each `attach_strategy`s itself to StateManager via the base class), prefetches warmup candles per unique `(symbol, interval)` via the warmup disk cache (see *WarmupCache* — the trailing still-forming candle is dropped via `market.drop_forming_candle`: REST klines always include it, but the kline WS only delivers closed candles, so a forming candle seeded into a buffer would stay stale until it finally closes), then calls `state_manager.start()` (sync resync + WS user-data stream + worker thread) and `strategy.adopt_pre_existing()` for each strategy to rehydrate any positions carried across restart. Finally opens a single WebSocket connection covering every `(symbol, interval)` pair and routes closed candles to matching strategies. On shutdown the live candle buffers are flushed back to the warmup cache. No strategy logic, no risk logic, no order placement.

### StateManager
WebSocket-driven source of truth for live state. An authenticated Binance user-data WebSocket (`utils/user_stream.UserDataStream`) pushes `ACCOUNT_UPDATE` (position/balance changes) and `ORDER_TRADE_UPDATE` (order lifecycle) events in real time. A single worker thread drains the event queue:
- **Event-driven symbol refresh:** on every relevant event, the affected symbol(s) are REST-refreshed (one position-info + one open-orders snapshot). `state.orders` is therefore always an authoritative REST snapshot, never an incrementally-reconstructed list — robust against dropped events and the algo-order migration.
- Builds a `SymbolState` per refreshed symbol (position side/size/entry/mark/unrealized P&L + all open orders).
- **Orphan reconciliation:** orders with no matching position are cancelled and a warning is logged.
- **Untracked-position warning:** a position with no exit orders is logged as a warning and left alone — StateManager never manages positions, only observes.
- Notifies subscribers (every `LiveTradeManager` is subscribed for its strategy's symbols) on each refresh of one of its symbols.
- A grace period (`state_manager.grace_period_secs`) suppresses orphan/untracked warnings briefly after a strategy calls `state_manager.mark_change(symbol)` (placed/cancelled orders).
- **Safety-net resync:** a full REST snapshot of every symbol runs every `state_manager.resync_interval_secs` and whenever the user-data socket reports a disconnect — corrects any drift from dropped events and covers the gap while the socket was down. The user-data stream runs on the SDK's `ThreadedWebsocketManager`, which owns the socket URL (testnet vs mainnet), the `listenKey` lifecycle (creation, keepalive, recreation on expiry), and reconnection; `UserDataStream` only filters its events and triggers a resync on each `error` / `listenKeyExpired`.
- Refreshes daily net P&L and trade count (via `account.get_futures_recent_trades` per symbol for the current UTC day) on every fill event and on every resync. Daily P&L resets at UTC midnight.
- Optionally drives a `DailyPnLReporter` (CSV + email) at UTC midnight.
- **Persistent ownership store (`state/positions.json`):** atomically rewritten on every state refresh. After updating `SymbolState`, prunes entries whose position is gone on Binance or whose owner strategy is no longer configured (warning logged for the latter). Strategies use `register_owner` / `update_owner` / `get_owner` to record and recover the strategy-specific state they need across restart. Binance is always the source of truth; the file is a cache.
- **Pending entry orders:** a strategy may place a resting limit ENTRY order — an order that legitimately has no position behind it yet. It calls `state_manager.register_pending_entry(symbol, order_id=..., on_fill=..., on_cancel=...)`, which (a) exempts `order_id` from orphan cancellation, (b) persists a `status="pending"` entry to the store, and (c) fires `on_fill(state)` when the order fills (a position appears) or `on_cancel(symbol)` when it vanishes unfilled (cancelled/rejected). Callbacks run on the worker thread. On fill, StateManager first re-writes the store entry with the **authoritative** `side`/`entry_price`/`qty` from the live snapshot and flips `status` to `"open"` *before* firing `on_fill` — so a crash before the strategy's `on_fill` finishes (placing exits, calling `register_owner`) still leaves a correct entry on disk for restart recovery. Pending entries are skipped by position-absence pruning (they have no position by design) but still dropped if their strategy is deconfigured. `clear_pending_entry(symbol)` drops one without firing callbacks (strategy cancelled the resting order itself). After a restart a strategy re-arms the exemption by re-calling `register_pending_entry` from `adopt`.

On `start()`, runs one synchronous resync before returning so callers see accurate state immediately (covers the restart-recovery case), then launches the worker thread and the WS user-data stream.

### PositionStore (`core/position_store.py`)
A thin JSON store keyed by symbol. Schema (versioned for future migrations):

```
{ "version": 1, "updated_at": "<UTC ISO>", "positions": {
    "<SYMBOL>": {
      "strategy": "<name>", "status": "open"|"pending", "opened_at": "<UTC ISO>",
      "side": "LONG"|"SHORT", "entry_price": "<dec>", "qty": "<dec>",
      "strategy_state": { ... opaque blob owned by strategy ... },
      "orders": { "stop_limit_id": <int>, "stop_market_id": <int>, "tp1_id": <int>, ... }
    }, ...
}}
```

`status` is `"open"` for a live position and `"pending"` for a resting limit
entry order with no position behind it yet (see *Pending entry orders* under
StateManager). The field is additive — entries written before it existed load as
`"open"`, so no schema-version bump was needed.

Writes use a write-to-temp-then-rename so crashes never leave a partial file. A corrupt or wrong-version file is quarantined (`<file>.corrupt-<ts>`) and the store starts empty. Lifecycle is owned by `StateManager` — no other module touches it.

### WarmupCache (`core/warmup_cache.py`)
A `WarmupCache` object backed by a single JSON file (`state/warmup_cache.json` by default) holding every `(symbol, interval)` candle buffer under string keys `"<SYMBOL>|<interval>"`. Configured by the `warmup_cache` block in `config.yaml` (`enabled`, `file`); constructed with `path=None` when disabled, in which case it always full-fetches and `update`/`save` are no-ops.

A cold start otherwise fetches every strategy buffer in full — a burst of weighted kline calls that, on the CloudFront-fronted **testnet** (`testnet.binancefuture.com` is a CNAME to AWS CloudFront), shares a rate-limit bucket with every other client on the same edge POP and reliably trips Binance's `-1003` IP ban regardless of the bot's own request volume. The cache makes a restart re-fetch only the candles that closed while the bot was down.

The object owns the whole lifecycle — `bot.py` calls only these three methods, no cache-vs-fetch logic leaks into the orchestrator:
- `get_warmup(client, symbol, interval, limit)` — the read-or-fetch entry point used by `warmup_strategies`. Reads the in-memory buffer (loaded from the file at construction), computes how many candles closed since (`elapsed = (now − last_open_time) // interval_ms`), and: `elapsed ≤ 1` → cache hit, no REST call; `1 < elapsed < limit` → gap-fetch only the missing candles and `_merge` (de-dup by `open_time`); no/short (`< limit`)/too-stale (`elapsed ≥ limit`) cache → full fetch. Result trimmed to `limit`, trailing forming candle dropped.
- `update(symbol, interval, candle)` — called from the bot's candle-routing loop on every closed candle; appends it to the in-memory buffer (replacing on duplicate `open_time`) and trims the oldest past `candle_limit`. Memory only.
- `save()` — atomically rewrites the whole file (write-to-temp-then-rename). Called by `bot._run` after warmup and on shutdown; `get_warmup` does **not** persist, so the candle-routing loop never blocks on disk I/O.
- The cache stores **only closed candles**; each buffer is capped at `candle_limit`, so the file is a fixed-size rolling window (~2 MB total) that never grows. A missing/corrupt/wrong-version file logs a warning and starts empty — the cache is an optimisation, never a correctness dependency.
- `scripts/prefetch_warmup.py` is a standalone entry point: it builds the configured strategies and runs `warmup_strategies` to populate the cache **without** starting the bot. Retryable independently of the bot lifecycle on a `-1003`, and can be run from a different IP — copy `state/warmup_cache.json` to the server and the bot starts with zero warmup REST.

### RiskGuard
Stateless gate. `allow_open(symbol, strategy)` returns False if:
- The symbol already has a position on Binance (one-per-symbol, absolute).
- The number of open positions is at `risk_guard.max_concurrent_positions`.
- Cumulative realized daily loss has reached `risk_guard.max_daily_loss_usdt`. When tripped, blocks all new entries for the rest of the UTC day, sends a single warning email via Resend, resumes next UTC day automatically.

### Strategy (ABC)
One instance per strategy entry in config (entries with `active: false` are skipped at build time; `active` defaults to `true` when omitted). Each strategy:
- Declares one or more `intervals` (derived from `params` such as `entry_interval` and `regime_interval`) — the bot subscribes to a WebSocket stream for every `(symbol, interval)` pair across all strategies.
- Owns a per-`(symbol, interval)` candle buffer (`self._buffers[symbol][interval]`).
- Implements `compute_signal(symbol, candles) -> Signal | None` — pure decision logic, returns a `Signal` with `entry_price`, `stop_loss_price`, `take_profit_price` for OPEN actions.
- Implements `execute_open(signal)` — owns the entry mechanics (IOC, market, layered limits, whatever) and places its own exit orders via broker primitives.
- Calls `state_manager.mark_change(symbol)` before/after placing orders to suppress orphan warnings during the grace window.
- After a fill, calls `state_manager.register_owner(symbol, ...)` with the strategy-specific state needed to resume management after a restart; updates that entry via `state_manager.update_owner(...)` whenever local state or order IDs change (e.g. extrema, trailing stop replacement).
- Overrides `serialize_state(symbol)` and `adopt(symbol, entry)` if it needs restart recovery. The base `adopt_pre_existing()` walks symbols, looks up the owner entry, and calls `adopt` for entries whose `strategy` matches `self.name`.
- Optionally owns a `LiveTradeManager` (per-strategy lifecycle hooks fired on each StateManager refresh — i.e. on user-data events and on each safety-net resync). Strategies whose lifecycle decisions are tied to closed candles skip the LTM and manage exits directly in `_tick`.

`on_candle(symbol, interval, candle)` is the only entry point the bot calls. The default `_tick(symbol, interval)` updates the buffer, checks `state_manager.has_position(symbol)`, runs `compute_signal`, and dispatches to `risk_guard.allow_open` / `execute_open`. Multi-interval strategies override `_tick` to coordinate across intervals (e.g. higher-TF regime filter + lower-TF execution).

### Layered stops (base helpers)
Every protective stop placed by a strategy is a PAIR managed via base helpers:
- A **stop-limit** at the desired stop price (`limit_id`) — preferred fill, pays nothing beyond slippage to the limit price. Limit price defaults to the trigger (`stop_limit_buffer_pct = 0`).
- A **stop-market backstop** `stop_market_backstop_pct` further from entry (`market_id`) — guarantees exit if the stop-limit gets skipped through or sits unfilled.

Both legs are reduceOnly. If the limit fills partially first, reduce-only sizes the backstop down automatically. The pair is tracked as a `LayeredStopIds(limit_id, market_id)` on each `_ManagedPosition`. Helpers:
- `_place_layered_stop(symbol, exit_side, qty, stop_price)` — places both legs; cancels any partial success and returns `None` if either fails (caller emergency-closes).
- `_replace_layered_stop(symbol, exit_side, qty, new_stop_price, old_ids)` — place-new-pair → cancel-old-pair (never unprotected).
- `_cancel_layered_stop(symbol, ids)` — best-effort cleanup.
- `_adopt_replace_layered_stop(...)` — restart-recovery; cancels any surviving leg and places a fresh pair sized to the current position.

Both active strategies use these for their initial stop, trail-stop replacement (adaptive), and break-even move (bb_rsi). Defaults live in `config.yaml` per strategy: `stop_limit_buffer_pct: 0.0`, `stop_market_backstop_pct: 0.1`.

### Limit entry primitives (base helpers)
A strategy can enter via a **resting limit order** — an order placed away from the market with no position behind it until it fills. Shared base helpers wrap the `StateManager` pending-entry machinery:
- `_place_limit_entry(symbol, side, qty, price, *, on_fill, on_cancel, strategy_state=None)` — places a GTC LIMIT order and registers it via `state_manager.register_pending_entry` (exempt from orphan cancellation, persisted with `status="pending"`). `strategy_state` is the opaque blob the strategy needs to place exits on fill / re-arm on restart. Returns the order id or `None`.
- `_cancel_limit_entry(symbol, order_id)` — cancels the resting order and calls `clear_pending_entry` (the no-callback path; the strategy chose to cancel, so `on_cancel` does not fire).
- `_rearm_limit_entry(symbol, order_id, side_label, entry_price, qty, *, on_fill, on_cancel, strategy_state=None)` — re-registers an already-resting order after a restart (no new order placed); called from `adopt` for `status="pending"` entries.

`on_fill(state)` / `on_cancel(symbol)` run on the StateManager worker thread. The strategy owns where to rest the order, when to expire/re-place it, and what exits to place on fill. `trend_pullback_limit` is the first strategy to use these.

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
  3. **Trailing stop update:** trail = `highest_close_since_entry` − `trail_atr_mult` × ATR (inverted for shorts). Only moved when more favorable. Uses the base `_replace_layered_stop` helper — new layered pair (stop-limit + stop-market backstop) is placed first, then both old legs are cancelled, so the position is never momentarily unprotected.
- **Position sizing:** `qty = notional_per_trade_usdt / entry_price`, rounded down to `step_size`. `leverage` is set per-symbol at startup from `params.leverage`.
- **Restart recovery:** positions opened by this strategy are persisted to `state/positions.json` via StateManager. `serialize_state` writes `entry_atr`, `r_distance`, `entry_candle_open_time`, and the running `highest_close` / `lowest_close`; `adopt` rehydrates `_ManagedPosition` and reconciles saved order IDs against live Binance orders. If either leg of the layered stop is missing on adopt (cancelled or filled during downtime), any surviving leg is cancelled and a fresh pair is placed at `entry_price ± r_distance` (warning logged). Missing TP1 is not re-created.

### Active strategy: `bb_rsi_mean_reversion`
Bollinger-Band + RSI mean-reversion system, intended to trade only in non-trending regimes. All three intervals (`macro_interval`, `regime_interval`, `entry_interval`) and every indicator period / threshold are configurable in `params`.

- **Macro bias filter (macro_interval, e.g. 1d):** checked first. `close > EMA_slow AND EMA_fast > EMA_slow` → UP (longs only). `close < EMA_slow AND EMA_fast < EMA_slow` → DOWN (shorts only). Anything else → NEUTRAL → skip all entries. Prevents taking both sides in the same range and getting stop-hunted in both directions by an underlying directional drift.
- **Regime filter (regime_interval, e.g. 4h):** range-only. Requires `ADX <= regime_adx_max_range` AND `|EMA_fast − EMA_slow| / close <= regime_ema_flatness_pct`. `ADX > regime_adx_min_trend` explicitly disqualifies; the band in between is the "gray zone" — no trade.
- **Entry gates (entry_interval, e.g. 30m, longs; shorts inverse):** band pierce within `pierce_lookback` recent bars (current/previous low OR close below `bb_lower`); `RSI < rsi_oversold`; current `close > bb_lower` (reclaim); bullish candle; `close > prev_close`; `volume <= volume_max_mult × volume_SMA` (no panic spike); pierce depth `(bb_lower − close)` ≤ `max_pierce_atr_mult × ATR`; `ATR <= atr_max_expansion_mult × ATR_SMA`.
- **SL/TP per signal:** the strategy picks the MORE CONSERVATIVE (closer-to-entry) of an ATR stop and a structure stop:
  - ATR stop = entry ∓ `stop_atr_mult × ATR` (default 1.0 — tighter than trend).
  - Structure stop = swing low/high over `structure_stop_lookback` bars, ∓ `structure_stop_buffer_atr_mult × ATR` for headroom.
  - Final stop = `max(atr_stop, structure_stop)` for longs / `min` for shorts.
  - R-distance is computed against the chosen stop, so dead-trade / hard-SL-close math always reflects actual risk.
  - TP1 = `bb_middle` at signal close, `tp1_size_pct` of qty (GTC reduce-only LIMIT). TP2 = opposite band at signal close, `tp2_size_pct` of qty (GTC reduce-only LIMIT; skipped if `tp2_size_pct == 0`).
- **Break-even SL move:** the first closed candle on which the position size has shrunk vs `initial_qty` (i.e. a TP has partially filled) triggers `_move_stop_to_break_even`: uses base `_replace_layered_stop` to place a new layered pair (stop-limit + stop-market backstop) at `entry_price ± break_even_offset_atr_mult × entry_atr` for the *remaining* qty, then cancels both old legs — never momentarily unprotected. The `stop_moved_to_be` flag is persisted so adopt-on-restart doesn't re-fire it.
- **Entry execution:** single-shot IOC LIMIT at the signal close. Binance fills whatever is available at the signal price or better; the remainder is cancelled immediately. If nothing fills, the strategy walks away — by design we'd rather miss the trade than buy after the bounce has already moved. Pays the taker fee on entry; TPs are maker-priced (GTC LIMIT reduce-only). No chasing, no retry, no resting order, so the orphan-cancel / grace-period concerns that apply to GTX never come up. SL + TPs are placed synchronously immediately after the IOC returns a non-zero `executed_qty` — zero unprotected window.
- **Exits managed inside the strategy on every closed entry-interval candle** (no LiveTradeManager). Two check groups, in order:
  1. **Trend invalidation** (exit on any): 4h `ADX > regime_adx_min_trend` (range thesis broken); `ATR > atr_max_expansion_mult × ATR_SMA` (volatility expansion); `max_outside_band_candles` consecutive closes outside the relevant band; `max_rsi_extreme_candles` consecutive RSI extremes against the trade; close beyond entry by ≥ `stop_atr_mult × entry_ATR` against the position (hard SL re-check on close).
  2. **Time exit:** unconditional after `time_exit_hard_candles`; soft exit after `time_exit_soft_candles` IF `touched_middle` is still False AND the current close is on the *wrong side of entry* ("trade simply isn't working" — cleaner than an arbitrary fraction-of-R threshold).
- **Position sizing:** `qty = notional_per_trade_usdt / entry_price`, rounded down to `step_size`. `leverage` is set per-symbol at startup from `params.leverage`.
- **Restart recovery:** `serialize_state` writes `entry_atr`, `r_distance`, `entry_candle_open_time`, `outside_band_streak`, `rsi_extreme_streak`, `touched_middle`, and `stop_moved_to_be`; `adopt` rehydrates `_ManagedPosition` and reconciles saved order IDs (`stop_limit_id` / `stop_market_id` / `tp1_id` / `tp2_id`) against live Binance orders. If either leg of the layered stop is missing, any surviving leg is cancelled and a fresh pair is placed at `entry_price ± r_distance`. Missing TPs are not recreated (their qty was already split off the original fill).

### Active strategy: `trend_pullback_limit`
Trend-pullback system entering via a **resting limit order** — the least restrictive of the three. The other two are confirmation strategies (wait for a candle to close proving the setup, stacking many gates); this one decides a level in advance and rests a passive maker order there, so it fires far more often. Intervals (`entry_interval`, `regime_interval`) and all indicator periods are configurable in `params`.

- **Regime filter (regime_interval, e.g. 4h):** one loose gate — `EMA_fast > EMA_slow AND close > EMA_slow` → longs only (shorts inverse).
- **Entry (entry_interval, e.g. 1h):** the pullback level is `EMA_fast`, optionally pushed `entry_offset_atr_mult × ATR` deeper. A resting GTC LIMIT is placed at the level via the base limit-entry helpers, but only if the current close is on the far side of it (`close > level` for longs) so the order rests as a maker rather than filling as a taker. ATR is frozen at placement and carried in the pending entry's `strategy_state`.
- **Resting-order lifecycle** (each closed entry-interval candle): cancelled if the regime flips against it, or if unfilled after `entry_expiry_candles`.
- **SL/TP per fill** (placed in the `on_fill` callback off the authoritative fill price): `R = stop_atr_mult × ATR`. Stop = fill ∓ R as a **layered pair** (stop-limit + stop-market backstop). TP1 = fill ± `tp1_r_multiple × R` (`tp1_size_pct` of qty, GTC reduce-only LIMIT). TP2 = fill ± `tp2_r_multiple × R` (`tp2_size_pct`, GTC reduce-only LIMIT; skipped if `tp2_size_pct == 0`).
- **Break-even SL move:** the first closed candle on which position size has shrunk vs `initial_qty` (a TP partial-filled) replaces the layered stop at entry (± `break_even_offset_atr_mult × ATR`) for the remaining qty — place-then-cancel, never unprotected. Once TP1 fills the trade can no longer become a net loser.
- No trailing stop and no dead-trade/invalidation gauntlet — the stop + two fixed-R TPs fully define each trade. No LiveTradeManager.
- **Position sizing:** `qty = notional_per_trade_usdt / resting_limit_price`, rounded down to `step_size`. `leverage` set per-symbol at startup.
- **Restart recovery:** `serialize_state` writes `entry_atr`, `r_distance`, `entry_candle_open_time`, `stop_moved_to_be`. `adopt` branches on the store entry's `status`: `"pending"` → re-arm the resting order via `_rearm_limit_entry` (or, if it filled during downtime, place the missing exits and adopt as open; or clear it if the order is gone); `"open"` → rehydrate `_ManagedPosition` and reconcile the layered stop + TP order IDs against live Binance orders, re-placing a missing layered stop pair.

### Multi-strategy on the same symbol
Symbol ownership is absolute and short-lived. The first strategy in `config.yaml.strategies` that fires on a given symbol opens the position; every other strategy sees `state_manager.has_position(symbol) == True` and stays silent until the position closes. Strategies do not coordinate directly — they coordinate through StateManager.

### WebSockets
One `_KlineStreamManager` (a SDK `ThreadedWebsocketManager` multiplex socket) covers every `(symbol, interval)` pair. The bot opens streams for `pairs = unique_intervals × symbols`. Gap recovery on every closed candle: if `new.open_time - last.open_time > interval_ms`, REST-fetches the missing range via `get_futures_ohlcv` and delivers the back-filled candles before the new one, logging a `[ws] gap-fill` warning. The socket manager owns the socket URL and reconnects automatically on disconnect; the gap-fill covers any candles missed during the outage.

### Restart recovery
Startup order in `bot.py`: build StateManager (loads `state/positions.json`) → build strategies (each calls `state_manager.attach_strategy` in the base `__init__`) → warmup → `state_manager.start()` (sync resync populates `_states`, prunes file entries whose Binance position is gone or whose strategy is no longer configured; then the WS user-data stream takes over) → `strategy.adopt_pre_existing()` per strategy (rehydrates internal state and reconciles order IDs against live Binance orders).

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
