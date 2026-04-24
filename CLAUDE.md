# Day Trading Bot

Context file for Claude Code. Read this before suggesting changes or generating code.

## Project goal

A day trading bot that places trades on Binance based on configurable strategies.

## Tech stack

- **Language:** Python 3.11+
- **Exchange API:** Binance (via the official `python-binance` client unless a reason arises to switch)
- **Logging:** loguru
- **Runtime:** Docker (single container, `docker compose` for local dev)
- **Config:** `.env` for secrets, a separate non-secret config file (e.g. `config.yaml`) for strategy parameters

## File layout

```
day-trading-bot/
├── bot.py              # Entry point — the actual bot loop / orchestration
├── strategies.py       # Trading strategies; consumes functions from utils
├── utils.py            # Binance API wrapper (trades, stop-loss, balances, etc.)
├── config.yaml         # Non-secret runtime config (symbols, thresholds, intervals)
├── .env                # Secrets ONLY (API key, API secret) — never committed, never baked into image
├── .env.example        # Template with placeholder values, safe to commit
├── .gitignore          # Must exclude .env, logs/, __pycache__, .venv, etc.
├── .dockerignore       # Must exclude .env, logs/, .git, .venv, __pycache__
├── Dockerfile
├── docker-compose.yml  # Mounts ./logs into the container
├── requirements.txt
├── logs/               # Log output; mounted as a volume, not in the image
└── tests/              # Unit tests, especially for strategies
```

## Hard rules

1. **Secrets never leave `.env`.** Do not hardcode API keys anywhere. `.env` must be in both `.gitignore` and `.dockerignore`. The Docker image must not contain `.env` — secrets are passed in at runtime (via `env_file:` in `docker-compose.yml` or `-e` flags).
2. **All Binance API calls go through `utils.py`.** `strategies.py` and `bot.py` must not import `binance` directly.
3. **Every API action and every bot decision is logged via loguru** to both stdout and a file in `logs/`. That file must be on a mounted volume so logs survive container restarts.
4. **The log file is not in the Docker image.** It's created at runtime in a mounted volume.

## Design decisions carried over from planning

- **Testnet vs mainnet toggle.** Config-driven (e.g. `BINANCE_TESTNET=true` in `.env`). Default to testnet for safety.
- **Dry-run mode.** A flag that makes the bot log what it *would* trade without actually calling the order endpoints. Useful during development.
- **Risk controls live in `bot.py`**, wrapping the strategy layer: max position size, max daily loss, and a kill switch. Strategies decide intent; the bot enforces limits before anything reaches `utils.py`.
- **Rate limiting and reconnection logic live in `utils.py`.** Retries with backoff are centralized so strategies don't each reinvent them.
- **State persistence on crash.** On startup, re-query Binance for open orders/positions rather than trusting a local cache. Any local state (e.g. last-seen candle) goes in a small SQLite or JSON file under a mounted volume.
- **Config separation.** Secrets in `.env`, strategy parameters (symbols, thresholds, intervals, risk limits) in `config.yaml`. Makes tweaking safe to commit.
- **Tests.** `tests/` folder with unit tests for the strategy layer at minimum. Mock `utils.py` rather than hitting Binance.

## Execution boundaries

- Claude must NOT run shell commands. Propose them in chat; the user runs them.
- Claude must NOT run git commands (including `git add`, `git commit`, `git push`). The user handles all version control.
- Claude may create, edit, and delete files in the project. The user reviews diffs and commits.
- If a task requires running code (tests, installs, Docker builds), Claude should output the exact command for the user to run, then wait for results.

## Coding conventions

- Type hints on all public functions.
- Docstrings on anything in `utils.py` and `strategies.py`.
- No `print()` — always loguru.
- Keep `bot.py` thin: it orchestrates, it doesn't implement strategy logic or API calls.

## When generating code

- If a new dependency is needed, add it to `requirements.txt` and explain why.
- If a change touches Docker, update both `Dockerfile` and `docker-compose.yml` consistently.
- If a change touches secrets handling, re-verify that `.gitignore` and `.dockerignore` still cover it.
- Prefer editing existing files over creating new ones unless a new module clearly belongs.
