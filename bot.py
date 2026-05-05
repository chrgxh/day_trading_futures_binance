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


class RiskGuard:
    """Enforces per-trade and daily loss limits before any order is placed."""

    def __init__(self, max_position_usdt: float, max_daily_loss_usdt: float, kill_switch: bool):
        self.max_position_usdt = Decimal(str(max_position_usdt))
        self.max_daily_loss_usdt = Decimal(str(max_daily_loss_usdt))
        self.kill_switch = kill_switch
        self.daily_loss: Decimal = Decimal("0")

    def check(self, signal: TradeSignal) -> bool:
        """Return True if the trade is permitted under current risk limits."""
        if self.kill_switch:
            logger.warning("Kill switch is active — all trades blocked.")
            return False
        if self.daily_loss >= self.max_daily_loss_usdt:
            logger.warning(
                "Daily loss limit reached ({} / {}). Blocking trade.",
                self.daily_loss, self.max_daily_loss_usdt,
            )
            return False
        return True

    def record_loss(self, amount_usdt: Decimal) -> None:
        self.daily_loss += amount_usdt
        logger.info("Daily loss updated: {} / {}", self.daily_loss, self.max_daily_loss_usdt)


def run() -> None:
    cfg = load_config()
    env = load_env()

    log_cfg = cfg["logging"]
    configure_logging(
        log_file=log_cfg["log_file"],
        level=log_cfg["level"],
        rotation=log_cfg["rotation"],
        retention=log_cfg["retention"],
    )

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
    strategy_name: str = cfg["trading"]["strategy"]
    strategy_params: dict = cfg["trading"].get("strategy_params", {})
    strategy_fn = STRATEGIES[strategy_name]
    logger.info("Strategy: {}", strategy_name)

    # Re-query Binance for open positions so state survives restarts
    open_positions: dict[str, Position] = {s: Position.NONE for s in symbols}
    for pos in account.get_futures_positions(client):
        if pos["symbol"] in open_positions:
            open_positions[pos["symbol"]] = Position[pos["side"]]
            logger.info("Recovered open {} position for {}", pos["side"], pos["symbol"])

    while True:
        for symbol in symbols:
            try:
                position = open_positions[symbol]
                candles = market.get_futures_ohlcv(client, symbol, interval)
                signal = strategy_fn(candles, symbol, position, strategy_params)

                logger.info("{} [{}] {} — {}", symbol, position.value, signal.signal.value, signal.reason)

                if signal.signal == Signal.HOLD:
                    continue

                if not risk.check(signal):
                    continue

                if signal.signal in (Signal.OPEN_LONG, Signal.OPEN_SHORT):
                    price = market.get_futures_mark_price(client, symbol)
                    quantity = (risk.max_position_usdt / price).quantize(Decimal("0.00001"))
                    side = "BUY" if signal.signal == Signal.OPEN_LONG else "SELL"
                    orders.place_market_order(client, symbol, side, quantity)
                    open_positions[symbol] = Position.LONG if signal.signal == Signal.OPEN_LONG else Position.SHORT

                elif signal.signal == Signal.CLOSE:
                    pos_utils.close_position(client, symbol)
                    open_positions[symbol] = Position.NONE

            except Exception as exc:
                logger.exception("Error processing {}: {}", symbol, exc)

        logger.debug("Loop complete. Sleeping {}s.", loop_sleep)
        time.sleep(loop_sleep)


if __name__ == "__main__":
    run()
