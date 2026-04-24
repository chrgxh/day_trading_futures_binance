# Day Trading Bot

Context file for Claude Code. Read this before suggesting changes or generating code.

## Project goal

A futures day trading bot that places trades on Binance Futures based on configurable strategies.

## Tech stack

- **Language:** Python 3.11+
- **Exchange API:** Binance Futures (via the official `python-binance` client)
- **Logging:** loguru
- **Runtime:** Docker (single container, `docker compose` for local dev)
- **Config:** `.env` for secrets, `config.yaml` for strategy parameters

## File layout

```
day-trading-bot/
├── bot.py                   # Entry point — bot loop, orchestration, risk controls
├── utils/
│   ├── __init__.py
│   ├── exchange.py          # Authenticated Binance actions (positions, orders, connection)
│   ├── market.py            # Public market data (OHLCV, mark price)
│   └── indicators.py        # Signal types and technical indicators (MA, etc.)
├── config.yaml              # Non-secret runtime config (symbols, intervals, risk limits)
├── .env                     # Secrets ONLY (API key, API secret) — never committed
├── .env.example             # Placeholder template, safe to commit
├── .gitignore
├── .dockerignore
├── Dockerfile
├── docker-compose.yml       # Mounts ./logs into the container
├── requirements.txt
├── logs/                    # Log output; mounted volume, not baked into image
└── tests/
    ├── test_exchange.py     # Unit tests for utils/exchange.py (mocked)
    ├── test_market.py       # Unit tests for utils/market.py (mocked)
    ├── test_indicators.py   # Unit tests for utils/indicators.py
    └── test_integration.py  # Live testnet tests (requires credentials, -m integration)
```

## Hard rules

1. **Secrets never leave `.env`.** Do not hardcode API keys anywhere. `.env` must be in both `.gitignore` and `.dockerignore`. Secrets are passed at runtime via `env_file:` in `docker-compose.yml`.
2. **All Binance API calls go through `utils/exchange.py` or `utils/market.py`.** `bot.py` and `utils/indicators.py` must not import `binance` directly.
3. **Futures only.** Use futures endpoints exclusively (`futures_*` methods on the client). No spot trading.
4. **Every API action and every bot decision is logged via loguru** to both stdout and a file in `logs/`. Logs must survive container restarts via a mounted volume.
5. **The log file is not in the Docker image.** It is created at runtime in the mounted volume.

## Design decisions

- **Testnet vs mainnet toggle.** `BINANCE_TESTNET=true` in `.env`. Default to testnet for safety.
- **Dry-run mode.** `DRY_RUN=true` in `.env`. Bot logs intended trades without calling order endpoints.
- **Risk controls live in `bot.py`** — max position size, max daily loss, kill switch. Indicators decide intent; `bot.py` enforces limits before anything reaches `utils/exchange.py`.
- **Retry with backoff lives in `utils/exchange.py`** via `with_retry()`. Centralised so nothing else reinvents it.
- **State persistence on crash.** On startup, re-query Binance for open positions rather than trusting a local cache.
- **Config separation.** Secrets in `.env`, everything else (symbols, intervals, risk limits) in `config.yaml`.

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
- Mock `utils/exchange.py` and `utils/market.py` in unit tests — never hit the real API.
- **After every change, check whether `CLAUDE.md` and `README.md` need updating.** If the file layout, hard rules, design decisions, dependencies, configuration, or usage instructions are affected — update them as part of the same task, not as a follow-up.
