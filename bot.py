"""Entry point. Orchestrates the bot loop and enforces risk controls."""

import os
import sys
import time
from decimal import Decimal

import yaml
from dotenv import load_dotenv
from loguru import logger

from utils import account, general, market, orders
from utils import positions as pos_utils
from utils.indicators import Position, Signal, TradeSignal
from strategies import STRATEGIES


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def configure_logging(log_file: str, level: str, rotation: str, retention: str) -> None:
    logger.remove()
    logger.add(sys.stdout, level=level)
    logger.add(log_file, level=level, rotation=rotation, retention=retention, enqueue=True)


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_env() -> dict:
    load_dotenv()
    required = ("BINANCE_API_KEY", "BINANCE_API_SECRET")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logger.error("Missing required environment variables: {}", missing)
        sys.exit(1)
    return {
        "api_key": os.environ["BINANCE_API_KEY"],
        "api_secret": os.environ["BINANCE_API_SECRET"],
        "testnet": os.getenv("BINANCE_TESTNET", "true").lower() == "true",
    }


def setup_symbols(client, symbols: list[str], leverage: int) -> dict[str, dict]:
    """Set leverage and fetch symbol info for every symbol."""
    sym_info = {}
    for symbol in symbols:
        account.set_leverage(client, symbol, leverage)
        sym_info[symbol] = account.get_symbol_info(client, symbol)
    return sym_info


def recover_positions(client, symbols: list[str]) -> dict[str, Position]:
    """Query Binance for open positions so state survives restarts."""
    open_positions = {s: Position.NONE for s in symbols}
    for pos in account.get_futures_positions(client):
        if pos["symbol"] in open_positions:
            open_positions[pos["symbol"]] = Position[pos["side"]]
            logger.info("Recovered open {} position for {}", pos["side"], pos["symbol"])
    return open_positions


def execute_signal(
    client,
    symbol: str,
    signal: TradeSignal,
    max_usdt: Decimal,
    step_size: Decimal,
    open_positions: dict[str, Position],
) -> None:
    """Place the order for a non-HOLD signal and update open_positions."""
    if signal.signal in (Signal.OPEN_LONG, Signal.OPEN_SHORT):
        price = market.get_futures_mark_price(client, symbol)
        quantity = (max_usdt / price // step_size) * step_size
        side = "BUY" if signal.signal == Signal.OPEN_LONG else "SELL"
        orders.place_market_order(client, symbol, side, quantity)
        open_positions[symbol] = Position.LONG if signal.signal == Signal.OPEN_LONG else Position.SHORT

    elif signal.signal == Signal.CLOSE:
        pos_utils.close_position(client, symbol)
        open_positions[symbol] = Position.NONE


# ---------------------------------------------------------------------------
# Risk controls
# ---------------------------------------------------------------------------

class RiskGuard:
    """Enforces per-trade and daily loss limits before any order is placed."""

    def __init__(self, max_position_usdt: float, max_daily_loss_usdt: float, kill_switch: bool):
        self.max_position_usdt = Decimal(str(max_position_usdt))
        self.max_daily_loss_usdt = Decimal(str(max_daily_loss_usdt))
        self.kill_switch = kill_switch
        self.daily_loss: Decimal = Decimal("0")

    def check(self) -> bool:
        """Return True if the trade is permitted under current risk limits."""
        if self.kill_switch:
            logger.warning("Kill switch is active — all trades blocked.")
            return False
        if self.daily_loss >= self.max_daily_loss_usdt:
            logger.warning("Daily loss limit reached ({} / {}). Blocking trade.",
                           self.daily_loss, self.max_daily_loss_usdt)
            return False
        return True

    def record_loss(self, amount_usdt: Decimal) -> None:
        self.daily_loss += amount_usdt
        logger.info("Daily loss updated: {} / {}", self.daily_loss, self.max_daily_loss_usdt)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    cfg = load_config()
    env = load_env()
    configure_logging(**cfg["logging"])

    logger.info("Bot starting. testnet={}", env["testnet"])

    client = general.build_client(env["api_key"], env["api_secret"], testnet=env["testnet"])
    risk = RiskGuard(
        max_position_usdt=cfg["risk"]["max_position_size_usdt"],
        max_daily_loss_usdt=cfg["risk"]["max_daily_loss_usdt"],
        kill_switch=cfg["risk"]["kill_switch"],
    )

    symbols: list[str] = cfg["trading"]["symbols"]
    interval: str = cfg["trading"]["interval"]
    loop_sleep: int = cfg["trading"]["loop_interval_seconds"]
    strategy_fn = STRATEGIES[cfg["trading"]["strategy"]]
    strategy_params: dict = cfg["trading"].get("strategy_params", {})
    logger.info("Strategy: {}", cfg["trading"]["strategy"])

    sym_info = setup_symbols(client, symbols, cfg["risk"]["leverage"])
    open_positions = recover_positions(client, symbols)

    while True:
        for symbol in symbols:
            try:
                position = open_positions[symbol]
                candles = market.get_futures_ohlcv(client, symbol, interval)
                signal = strategy_fn(candles, symbol, position, strategy_params)

                logger.info("{} [{}] {} — {}", symbol, position.value, signal.signal.value, signal.reason)

                if signal.signal == Signal.HOLD or not risk.check():
                    continue

                execute_signal(client, symbol, signal, risk.max_position_usdt,
                               sym_info[symbol]["step_size"], open_positions)

            except Exception as exc:
                logger.exception("Error processing {}: {}", symbol, exc)

        logger.debug("Loop complete. Sleeping {}s.", loop_sleep)
        time.sleep(loop_sleep)


if __name__ == "__main__":
    run()
