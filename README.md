# Binance Futures Trading Bot

Automated futures trading bot for Binance. Evaluates technical indicators on a configurable interval and places futures orders when a signal triggers, subject to risk controls.

## Structure

```
bot.py                   — main loop, orchestration, risk controls
utils/
  general.py             — shared primitives: build_client, with_retry, order normalizers
  account.py             — account state: connection, balances, positions, symbol info, leverage
  orders.py              — regular orders: market, limit, get_open_orders, cancel, cancel_all
  algo_orders.py         — conditional orders: stop/TP market and limit variants, cancel_algo
  positions.py           — position management: close_position
  market.py              — public data: OHLCV candles, mark price
  indicators.py          — signal types (Signal, TradeSignal) and indicators (MA crossover)
config.yaml              — symbols, interval, risk limits (safe to commit)
.env                     — mainnet API keys and runtime flags (never commit)
.env.testnet             — testnet API keys for integration tests (never commit)
tests/
  integration/           — testnet integration tests, one file per module
logs/                    — runtime log output, mounted volume
sandbox.ipynb            — manual testnet notebook for ad-hoc scenario testing
```

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=true   # set to false for mainnet
DRY_RUN=true           # set to false to place real orders
```

For integration tests, copy `.env.testnet.example` to `.env.testnet` and fill in your testnet credentials.

Strategy parameters (symbols, interval, risk limits) live in `config.yaml`.

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

Integration tests run against the live Binance testnet and require `.env.testnet`:

```bash
pytest -m integration
```

Run a specific module's tests:

```bash
pytest tests/integration/test_orders.py -m integration -v
pytest tests/integration/test_algo_orders.py -m integration -v
pytest tests/integration/test_positions.py -m integration -v
```

Plain `pytest` (no `-m integration`) skips all integration tests.

## Safety defaults

- `BINANCE_TESTNET=true` — connects to testnet by default
- `DRY_RUN=true` — logs intended trades without placing orders
- Kill switch in `config.yaml` (`risk.kill_switch: true`) blocks all trades instantly
- Max position size and max daily loss enforced in `bot.py` before any order reaches the exchange
