"""Entry point. Orchestrates the bot loop and enforces risk controls."""

import os
import sys
import time
from decimal import Decimal

import yaml
from dotenv import load_dotenv
from loguru import logger

from utils import general, market, indicators


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def configure_logging(log_file: str, level: str, rotation: str, retention: str) -> None:
    logger.remove()
    logger.add(sys.stdout, level=level)
    logger.add(log_file, level=level, rotation=rotation, retention=retention, enqueue=True)


# ---------------------------------------------------------------------------
# Config / env loading
# ---------------------------------------------------------------------------

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
        "dry_run": os.getenv("DRY_RUN", "true").lower() == "true",
    }


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

    def check(self, signal: indicators.TradeSignal, price: Decimal) -> bool:
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


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

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

    logger.info("Bot starting. testnet={} dry_run={}", env["testnet"], env["dry_run"])

    client = general.build_client(env["api_key"], env["api_secret"], testnet=env["testnet"])

    risk = RiskGuard(
        max_position_usdt=cfg["risk"]["max_position_size_usdt"],
        max_daily_loss_usdt=cfg["risk"]["max_daily_loss_usdt"],
        kill_switch=cfg["risk"]["kill_switch"],
    )

    symbols: list[str] = cfg["trading"]["symbols"]
    interval: str = cfg["trading"]["interval"]
    loop_sleep: int = cfg["trading"]["loop_interval_seconds"]

    while True:
        for symbol in symbols:
            try:
                candles = market.get_futures_ohlcv(client, symbol, interval)
                signal = indicators.moving_average_crossover(candles, symbol)

                logger.info("{} signal: {} — {}", symbol, signal.signal.value, signal.reason)

                if signal.signal == indicators.Signal.HOLD:
                    continue

                price = market.get_futures_mark_price(client, symbol)
                if not risk.check(signal, price):
                    continue

                quantity = (risk.max_position_usdt / price).quantize(Decimal("0.00001"))
                logger.info(
                    "[{}] {} {} qty={}",
                    "DRY RUN" if env["dry_run"] else "LIVE",
                    signal.signal.value, symbol, quantity,
                )
                # TODO: place futures order via exchange module

            except Exception as exc:
                logger.exception("Error processing {}: {}", symbol, exc)

        logger.debug("Loop complete. Sleeping {}s.", loop_sleep)
        time.sleep(loop_sleep)


if __name__ == "__main__":
    run()
