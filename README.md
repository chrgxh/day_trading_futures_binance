# Binance Futures Trading Bot

Automated futures trading bot for Binance. Subscribes to Binance Futures kline WebSocket streams and evaluates technical indicators on every candle close, placing orders when a signal triggers subject to risk controls.

## Structure

```
bot.py                   — main loop, orchestration, risk controls
strategies.py            — pluggable strategy functions + STRATEGIES registry
utils/
  general.py             — shared primitives: build_client, with_retry, send_crash_email, order normalizers
  account.py             — account state: connection, balances, positions, symbol info, leverage
  orders.py              — regular orders: market, limit, get_open_orders, cancel, cancel_all
  algo_orders.py         — conditional orders: stop/TP market and limit variants, cancel_algo
  positions.py           — position management: close_position
  market.py              — public data: OHLCV candles, mark price, WebSocket kline streams
  indicators.py          — signal types (Signal, TradeSignal) and raw indicators (SMA, EMA, MACD, ADX, RSI, resample_to_1h)
config.yaml              — symbols, interval, candle_limit, risk limits, strategy selection (safe to commit)
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
| `ema_trend_momentum` | **(active)** 15m EMA crossover gated by 1h 200 EMA trend, RVOL spike, and RSI momentum band. No fresh crossover required — any tick where all gates pass opens a trade, so cold-starts and post-close re-entries are immediate. Requires `candle_limit: 840`. |
| `ma_crossover` | Simple SMA crossover (fast period vs slow period). Requires `candle_limit: 200`. |

To add a new strategy: write a function `(candles, symbol, position, params) -> TradeSignal` in [strategies.py](strategies.py) and register it in `STRATEGIES`.

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
pytest tests/integration/test_notifications.py -m integration -v
```

Plain `pytest` (no `-m integration`) runs unit tests only and skips all integration tests.

## Safety defaults

- `BINANCE_TESTNET=true` — connects to testnet by default
- Kill switch in `config.yaml` (`risk.kill_switch: true`) blocks all trades instantly
- Max position size and max daily loss enforced in `bot.py` before any order reaches the exchange
- **GTX (Post-Only) limit entry** — positions are opened with a maker-only limit order instead of a market order:
  - `entry.limit_order_timeout_secs` (default `20`) — seconds to wait for a fill before cancelling and retrying
  - `entry.max_price_deviation_pct` (default `0.3`) — abort entry if price drifts more than this % from the signal price
  - `entry.max_retries` (default `3`) — max placement attempts before giving up on the signal
  - GTX orders are auto-cancelled by Binance if they would fill as a taker; the bot retries at the new price
- Dual stop losses placed automatically on every position open:
  - `risk.stop_loss_limit_pct` (default `1.5`) — stop-limit, preferred exit with less slippage
  - `risk.stop_loss_market_pct` (default `1.8`) — stop-market, safety net if price gaps past the limit
  - Both are cancelled automatically when the strategy signals a close
- Trailing take profit placed automatically on every position open (Binance `TRAILING_STOP_MARKET`):
  - `risk.trailing_take_profit_activation_pct` (default `3.0`) — % move in profit before trailing activates
  - `risk.trailing_take_profit_callback_rate` (default `0.5`) — % reversal from peak that triggers the close
  - Cancelled automatically when the strategy signals a close
