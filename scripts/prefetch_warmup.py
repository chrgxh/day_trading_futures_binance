"""Standalone warmup-cache prefetch — populates `state/warmup_cache/` without
running the bot.

Run it ahead of (or on a schedule alongside) the bot: the bot's next start then
re-fetches only the candles that closed since this ran, or none at all. Because
it is decoupled from the bot lifecycle it can be retried freely on a -1003 ban,
and it can be run from a different machine/IP — copy the resulting
`state/warmup_cache/` to the server and the bot starts with zero warmup REST,
sidestepping the testnet CloudFront-POP rate-limit ban entirely.

Usage:  python -m scripts.prefetch_warmup
"""

import sys

from loguru import logger

from bot import build_strategies, load_config, load_env, warmup_strategies
from core.risk_guard import RiskGuard
from core.state_manager import StateManager
from core.warmup_cache import WarmupCache
from utils import account, general


def main() -> None:
    """Build the configured strategies and prefetch their warmup candle cache."""
    cfg = load_config()
    env = load_env()
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    logger.info("Warmup-cache prefetch starting. testnet={}", env["testnet"])

    client = general.build_client(env["api_key"], env["api_secret"], testnet=env["testnet"])
    symbols: list[str] = cfg["symbols"]
    # Symbol info only — unlike the bot, this does not touch leverage.
    sym_infos = account.get_symbol_infos(client, symbols)

    # StateManager / RiskGuard are constructed solely to satisfy build_strategies;
    # nothing is started, so no WebSocket or resync runs.
    sm_cfg = cfg.get("state_manager", {})
    state_manager = StateManager(
        client, symbols, testnet=env["testnet"],
        positions_file=sm_cfg.get("positions_file"),
    )
    rg_cfg = cfg["risk_guard"]
    risk_guard = RiskGuard(
        state_manager=state_manager,
        max_concurrent_positions=int(rg_cfg["max_concurrent_positions"]),
        max_daily_loss_usdt=float(rg_cfg["max_daily_loss_usdt"]),
    )
    strategies = build_strategies(
        cfg, client=client, sym_infos=sym_infos,
        state_manager=state_manager, risk_guard=risk_guard, symbols=symbols,
    )

    wc_cfg = cfg.get("warmup_cache", {}) or {}
    cache_path = wc_cfg.get("file", "state/warmup_cache.json")
    cache = WarmupCache(cache_path)
    warmup_strategies(client, strategies, symbols, cache)
    cache.save()
    logger.info("Warmup-cache prefetch complete -> {}", cache_path)


if __name__ == "__main__":
    main()
