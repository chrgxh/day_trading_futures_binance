# Binance Futures Trading Bot

Automated futures trading bot for Binance. Subscribes to Binance Futures kline WebSocket streams and evaluates technical indicators on every candle close, placing orders when a signal triggers subject to risk controls.

## Structure

```
bot.py                   — main loop, orchestration, risk controls
strategies.py            — pluggable strategy functions + STRATEGIES registry
utils/
  general.py             — shared primitives: build_client, with_retry, send_crash_email, order normalizers
  account.py             — account state: connection, balances, positions, symbol info, leverage, recent trades
  orders.py              — regular orders: market, limit, tp_limit, get_open_orders, cancel, cancel_all
  algo_orders.py         — conditional orders: stop/TP market and limit variants, cancel_algo
  positions.py           — position management: close_position
  market.py              — public data: OHLCV candles, mark price, WebSocket kline streams
  trade_manager.py       — background trade state manager: monitors positions, reconciles orders on external fills
  indicators.py          — signal types (Signal, TradeSignal) and raw indicators (SMA, EMA, MACD, ADX, RSI, resample_to_1h)
config.yaml              — symbols, interval, risk limits, strategy selection (safe to commit)
.env                     — mainnet API keys and runtime flags (never commit)
.env.testnet             — testnet API keys for integration tests (never commit)
tests/
  unit/                  — fast unit tests (no network), run with plain pytest
  integration/           — testnet integration tests, one file per module
logs/                    — runtime log output, mounted volume
sandbox.ipynb            — manual testnet notebook for ad-hoc scenario testing
```

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=true        # set to false for mainnet

# optional — crash email notifications via Resend
RESEND_API_KEY=
CRASH_NOTIFY_EMAIL=
CRASH_NOTIFY_FROM_EMAIL=    # must be a verified sender domain in Resend
```

For integration tests, copy `.env.testnet.example` to `.env.testnet` and fill in your testnet credentials (including the Resend vars if you want to run `test_notifications`).

Strategy selection and parameters live in `config.yaml` under `trading.strategy` and `trading.strategy_params`. Available strategies:

| Key | Description |
|---|---|
| `ema_trend_momentum` | **(active)** EMA crossover gated by 1h 200 EMA trend, RVOL spike, RSI momentum band, and ADX regime filter (ADX < `min_adx` blocks entry in ranging markets). No fresh crossover required — any tick where all gates pass opens a trade, so cold-starts and post-close re-entries are immediate. |
| `ma_crossover` | Simple SMA crossover (fast period vs slow period). |

To add a new strategy: write a function `(candles, symbol, position, params) -> TradeSignal` in [strategies.py](strategies.py) and register it in `STRATEGIES`.

**Changing the interval** (`trading.interval` in `config.yaml`): the bot auto-computes the candle prefetch limit and paginates REST requests when needed. Strategy params (`fast_period`, `slow_period`, `rsi_period`, `volume_lookback`) are used as-is — update them manually when switching intervals. `config.yaml` contains a tuning guide with recommended values for 1m / 5m / 15m / 1h / 4h.

## Running

**Locally:**
```bash
python bot.py
```

**Docker:**
```bash
docker compose up --build
```

## Testing

Unit tests run without any network connection:

```bash
pytest tests/unit/
```

Integration tests run against the live Binance testnet and require `.env.testnet`:

```bash
pytest -m integration
```

Run a specific module's tests:

```bash
pytest tests/integration/test_orders.py -m integration -v
pytest tests/integration/test_algo_orders.py -m integration -v
pytest tests/integration/test_positions.py -m integration -v
pytest tests/integration/test_trade_manager.py -m integration -v
pytest tests/integration/test_notifications.py -m integration -v
```

Plain `pytest` (no `-m integration`) runs unit tests only and skips all integration tests.

## Safety defaults

- `BINANCE_TESTNET=true` — connects to testnet by default
- Kill switch in `config.yaml` (`risk.kill_switch: true`) blocks all trades instantly
- Max position size and max daily loss enforced in `bot.py` before any order reaches the exchange
- **Two-stage limit entry** — GTX (post-only) maker attempt first, IOC taker chase as fallback:
  - `entry.gtx_timeout_secs` (default `5`) — seconds to wait per GTX attempt before cancelling and retrying
  - `entry.gtx_attempts` (default `3`) — number of GTX attempts before switching to IOC chase
  - `entry.max_price_deviation_pct` (default `0.3`) — abort entry if price drifts more than this % from the signal price; applies to both stages
  - IOC chase retries indefinitely at the current best ask/bid until filled or deviation limit is hit
- Four exit orders placed automatically on every position open:
  - `risk.stop_loss_limit_pct` (default `1.5`) — stop-limit, preferred SL exit with less slippage
  - `risk.stop_loss_market_pct` (default `1.8`) — stop-market, safety net if price gaps past the limit
  - `risk.take_profit_limit_pct` (default `3.0`) — maker GTC limit TP sitting on the book at +3% (long) / -3% (short) from entry; earns the maker rebate and catches wicks
  - `risk.trailing_take_profit_activation_pct` / `risk.trailing_take_profit_callback_rate` — trailing stop that activates at 3% profit and triggers on a 0.5% reversal from peak
  - All four are cancelled automatically when the strategy signals a close
- `TradeManager` polls Binance every `trade_manager.poll_interval_secs` (default `10`) seconds per tracked symbol. Silent when nothing changes. On external close: identifies which exit order fired, cancels only the remaining leftover orders, verifies they're gone, and logs realized P&L (WIN/LOSS). On partial TP fill: re-places stop orders at the reduced size, verifies the new orders are live, and logs cumulative P&L

## Strategy notes

`theory/gates.md` — explains what each entry gate does, why it exists, and its known limitations (lag, false positives, tuning levers). Useful reference when reviewing filtered trades or adjusting thresholds.
