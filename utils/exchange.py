"""Authenticated Binance actions — all calls that require API credentials."""

import time
from decimal import Decimal
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger


def build_client(api_key: str, api_secret: str, testnet: bool = True) -> Client:
    """Create and return an authenticated Binance client.

    Args:
        api_key: Binance API key.
        api_secret: Binance API secret.
        testnet: If True, connects to the testnet endpoint.

    Returns:
        Authenticated Binance Client instance.
    """
    client = Client(api_key, api_secret, testnet=testnet)
    logger.info("Binance client initialised (testnet={})", testnet)
    return client


def check_connection(client: Client) -> bool:
    """Ping Binance and log the server time. Returns True if reachable.

    Args:
        client: Authenticated Binance client.

    Returns:
        True on success, False on any API or network error.
    """
    try:
        client.futures_ping()
        ts = client.futures_time()
        logger.info("Binance Futures connection OK — server time: {}", ts["serverTime"])
        return True
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("Binance Futures connection check failed: {}", exc)
        return False


def get_open_positions(client: Client, symbol: Optional[str] = None) -> list[dict]:
    """Return all open futures positions (non-zero positionAmt).

    Args:
        client: Authenticated Binance client.
        symbol: Optional trading pair to filter by, e.g. "BTCUSDT". If None,
            returns all symbols with an open position.

    Returns:
        List of dicts with keys: symbol, side (LONG/SHORT), amount, entry_price,
        mark_price, unrealized_pnl, leverage, liquidation_price (all prices Decimal).
    """
    try:
        kwargs = {"symbol": symbol} if symbol else {}
        raw = client.futures_position_information(**kwargs)
        positions = []
        for p in raw:
            amt = Decimal(p["positionAmt"])
            if amt == 0:
                continue
            positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "amount": amt,
                "entry_price": Decimal(p["entryPrice"]),
                "mark_price": Decimal(p["markPrice"]),
                "unrealized_pnl": Decimal(p["unRealizedProfit"]),
                "leverage": int(p["leverage"]),
                "liquidation_price": Decimal(p["liquidationPrice"]),
            })
        logger.info("Open futures positions: {}", len(positions))
        return positions
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_open_positions failed: {}", exc)
        raise


def with_retry(fn, retries: int = 3, backoff: float = 2.0):
    """Call fn(), retrying up to `retries` times with exponential backoff.

    Args:
        fn: Zero-argument callable to attempt.
        retries: Maximum number of attempts.
        backoff: Base sleep seconds between attempts (doubles each retry).

    Returns:
        Return value of fn() on success.

    Raises:
        The last exception raised by fn() after all retries are exhausted.
    """
    delay = backoff
    last_exc: Exception = RuntimeError("with_retry called with retries=0")
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except (BinanceAPIException, BinanceRequestException) as exc:
            last_exc = exc
            logger.warning("Attempt {}/{} failed: {}. Retrying in {}s.", attempt, retries, exc, delay)
            time.sleep(delay)
            delay *= 2
    logger.error("All {} retries exhausted.", retries)
    raise last_exc
