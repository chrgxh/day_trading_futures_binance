"""Bot entry point. Orchestration only — no strategy or risk logic.

Startup order:
  1. Load config + env, configure logging, build Binance client.
  2. Fetch symbol info (one batched exchange-info call).
  3. Build StateManager (WS user-data stream + periodic REST resync).
  4. Build RiskGuard.
  5. Build configured strategies (each with an optional LiveTradeManager).
  6. Warmup: prefetch REST candles for every unique (symbol, interval) pair.
  7. Subscribe to one WebSocket connection covering all (symbol, interval) pairs.
  8. Main thread blocks on a queue and routes closed candles to matching strategies.

Crashes invoke utils.general.send_crash_email; per-strategy errors are caught per tick.
"""

import os
import queue
import signal
import sys
import threading
from typing import Iterable

import yaml
from dotenv import load_dotenv
from loguru import logger

from core.risk_guard import RiskGuard
from core.state_manager import StateManager
from core.strategies import STRATEGIES
from core.strategies.base import Strategy
from core.strategies.live_trade_manager import LiveTradeManager
from utils import account, general, market
from utils.indicators import interval_to_minutes


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def configure_logging(log_file: str, level: str, rotation: str, retention: str,
                      debug_log_file: str | None = None) -> None:
    logger.remove()
    logger.add(sys.stdout, level=level)
    logger.add(log_file, level=level, rotation=rotation, retention=retention, enqueue=True)
    if debug_log_file:
        logger.add(debug_log_file,
                   filter=lambda r: r["level"].no < logger.level("INFO").no,
                   rotation=rotation, retention=retention, enqueue=True)


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_env() -> dict:
    env_path = ".env"
    if os.path.exists(env_path):
        load_dotenv(env_path)
    required = ("BINANCE_API_KEY", "BINANCE_API_SECRET",
                "RESEND_API_KEY", "CRASH_NOTIFY_EMAIL", "CRASH_NOTIFY_FROM_EMAIL")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logger.error("Bot cannot start — missing env vars: {}", missing)
        sys.exit(1)
    return {
        "api_key": os.environ["BINANCE_API_KEY"],
        "api_secret": os.environ["BINANCE_API_SECRET"],
        "testnet": os.getenv("BINANCE_TESTNET", "true").lower() == "true",
    }


def setup_symbols(client, symbols: list[str], leverage: int) -> dict[str, dict]:
    for s in symbols:
        account.set_leverage(client, s, leverage)
    return account.get_symbol_infos(client, symbols)


def build_strategies(
    cfg: dict,
    *,
    client,
    sym_infos: dict[str, dict],
    state_manager: StateManager,
    risk_guard: RiskGuard,
    symbols: list[str],
) -> list[Strategy]:
    strategies: list[Strategy] = []
    for entry in cfg["strategies"]:
        name = entry["name"]
        if not entry.get("active", True):
            logger.info("Skipping strategy {} (active: false)", name)
            continue
        if name not in STRATEGIES:
            raise ValueError(f"Unknown strategy: {name!r} (available: {list(STRATEGIES)})")
        ltm_cfg = entry.get("live_trade_manager", {}) or {}
        ltm = LiveTradeManager(params=ltm_cfg.get("params", {})) if ltm_cfg.get("enabled") else None
        cls = STRATEGIES[name]
        strategy = cls(
            name=name,
            symbols=symbols,
            params=entry.get("params", {}),
            client=client,
            sym_infos=sym_infos,
            state_manager=state_manager,
            risk_guard=risk_guard,
            live_trade_manager=ltm,
        )
        strategies.append(strategy)
        logger.info("Built strategy {} @ {} (ltm={})", name, strategy.intervals, ltm is not None)
    return strategies


def unique_intervals(strategies: Iterable[Strategy]) -> list[str]:
    seen: list[str] = []
    for s in strategies:
        for interval in s.intervals:
            if interval not in seen:
                seen.append(interval)
    return seen


def warmup_strategies(client, strategies: list[Strategy], symbols: list[str]) -> None:
    # Group (strategy, interval) requests by interval; fetch enough candles per
    # symbol to satisfy the most-demanding strategy at that interval.
    by_interval: dict[str, list[Strategy]] = {}
    for s in strategies:
        for interval in s.intervals:
            by_interval.setdefault(interval, []).append(s)
    for interval, ss in by_interval.items():
        limit = max(s.candle_limit(interval) for s in ss)
        logger.info("Warmup @ {}: fetching {} candles per symbol", interval, limit)
        for symbol in symbols:
            candles = market.get_futures_ohlcv(client, symbol, interval, limit=limit)
            for s in ss:
                s.warmup(symbol, interval, candles)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    try:
        _run()
    except Exception as exc:
        logger.critical("Bot crashed: {}", exc)
        general.send_crash_email(exc)
        raise


def _run() -> None:
    cfg = load_config()
    env = load_env()
    configure_logging(**cfg["logging"])
    logger.info("Bot starting. testnet={}", env["testnet"])

    client = general.build_client(env["api_key"], env["api_secret"], testnet=env["testnet"])
    symbols: list[str] = cfg["symbols"]
    leverage = int(cfg.get("leverage", 20))
    sym_infos = setup_symbols(client, symbols, leverage)

    sm_cfg = cfg.get("state_manager", {})
    pnl_cfg = sm_cfg.get("pnl_reporter", {}) or {}
    pnl_reporter = None
    if pnl_cfg.get("enabled", True):
        from core.pnl_reporter import DailyPnLReporter
        pnl_reporter = DailyPnLReporter(client, symbols,
                                        pnl_cfg.get("csv_file", "logs/pnl.csv"),
                                        cfg["logging"]["log_file"])

    state_manager = StateManager(
        client, symbols,
        testnet=env["testnet"],
        resync_interval_secs=int(sm_cfg.get("resync_interval_secs", 90)),
        grace_period_secs=int(sm_cfg.get("grace_period_secs", 15)),
        pnl_reporter=pnl_reporter,
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
    warmup_strategies(client, strategies, symbols)

    # Strategies are now built (state_manager.attach_strategy called for each)
    # and warmed up. Start the state manager — the first sync resync prunes
    # entries whose position is gone on Binance or whose strategy is no longer
    # configured, then the WS user-data stream takes over.
    state_manager.start()

    # With _states populated from the first sync resync, each strategy can adopt
    # its persisted positions and reconcile saved order IDs against live state.
    for s in strategies:
        s.adopt_pre_existing()

    pairs = [(sym, interval) for interval in unique_intervals(strategies) for sym in symbols]
    event_queue: queue.SimpleQueue = queue.SimpleQueue()
    shutdown = threading.Event()

    def on_closed_candle(sym: str, interval: str, candle: dict) -> None:
        event_queue.put((sym, interval, candle))

    def signal_handler(signum, _frame):
        logger.info("Received signal {} — shutting down.", signum)
        shutdown.set()
        event_queue.put(None)  # unblock event_queue.get()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    twm = market.start_kline_streams(client, env["testnet"], pairs, on_closed_candle)

    by_interval: dict[str, list[Strategy]] = {}
    for s in strategies:
        for interval in s.intervals:
            by_interval.setdefault(interval, []).append(s)

    try:
        while not shutdown.is_set():
            item = event_queue.get()
            if item is None:
                break
            symbol, interval, candle = item
            for s in by_interval.get(interval, []):
                if symbol in s.symbols:
                    s.on_candle(symbol, interval, candle)
    finally:
        state_manager.stop()
        twm.stop()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    run()
