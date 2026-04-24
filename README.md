# Binance Futures Trading Bot

Automated futures trading bot for Binance. Evaluates technical indicators on a configurable interval and places futures orders when a signal triggers, subject to risk controls.

## Structure

```
bot.py              — main loop, risk controls (RiskGuard)
utils/
  exchange.py       — authenticated actions: positions, orders, connection
  market.py         — public data: OHLCV candles, mark price
  indicators.py     — signal types (Signal, TradeSignal) and indicators (MA crossover)
config.yaml         — symbols, interval, risk limits (safe to commit)
.env                — API keys and runtime flags (never commit)
tests/              — unit tests (mocked) + integration tests (testnet)
logs/               — runtime log output, mounted volume
```

## Configuration

Copy `.env.example` to `.env` and fill in your testnet credentials:

```
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=true   # set to false for mainnet
DRY_RUN=true           # set to false to place real orders
```

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

Unit tests (no network required):
```bash
pytest tests/test_exchange.py tests/test_market.py tests/test_indicators.py -v
```

Live testnet integration tests:
```bash
pytest tests/test_integration.py -v -m integration
```

## Safety defaults

- `BINANCE_TESTNET=true` — connects to testnet by default
- `DRY_RUN=true` — logs intended trades without placing orders
- Kill switch in `config.yaml` (`risk.kill_switch: true`) blocks all trades instantly
- Max position size and max daily loss enforced in `bot.py` before any order reaches the exchange
