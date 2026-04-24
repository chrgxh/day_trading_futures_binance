"""Binance API wrapper. All exchange interactions go through this module."""

import time
from decimal import Decimal
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from binance.helpers import date_to_milliseconds
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


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------

def check_connection(client: Client) -> bool:
    """Ping Binance and log the server time. Returns True if reachable.

    Args:
        client: Authenticated Binance client.

    Returns:
        True on success, False on any API or network error.
    """
    try:
        client.ping()
        ts = client.get_server_time()
        logger.info("Binance connection OK — server time: {}", ts["serverTime"])
        return True
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("Binance connection check failed: {}", exc)
        return False


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def get_ohlcv(
    client: Client,
    symbol: str,
    interval: str,
    limit: int = 100,
    start_str: Optional[str] = None,
    end_str: Optional[str] = None,
) -> list[dict]:
    """Fetch OHLCV candle data as a list of named dicts.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        interval: Kline interval, e.g. "1m", "5m", "1h", "1d".
        limit: Max candles to return (1–1000). Ignored when start_str is set
            and the date range contains more candles than the limit.
        start_str: Optional start time parseable by Binance helpers, e.g.
            "1 Jan 2024", "2024-01-01", "2 hours ago UTC".
        end_str: Optional end time in the same format as start_str.

    Returns:
        List of dicts with keys: open_time, open, high, low, close, volume,
        close_time. Prices and volume are Decimal.
    """
    try:
        kwargs: dict = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_str is not None:
            kwargs["startTime"] = date_to_milliseconds(start_str)
        if end_str is not None:
            kwargs["endTime"] = date_to_milliseconds(end_str)
        raw = client.get_klines(**kwargs)
        candles = [
            {
                "open_time": row[0],
                "open": Decimal(row[1]),
                "high": Decimal(row[2]),
                "low": Decimal(row[3]),
                "close": Decimal(row[4]),
                "volume": Decimal(row[5]),
                "close_time": row[6],
            }
            for row in raw
        ]
        logger.debug("Fetched {} OHLCV candles for {} @ {}", len(candles), symbol, interval)
        return candles
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_ohlcv failed for {}: {}", symbol, exc)
        raise


def get_symbol_ticker(client: Client, symbol: str) -> Decimal:
    """Return the latest futures mark price for a symbol as a Decimal.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".

    Returns:
        Current mark price as Decimal.
    """
    try:
        ticker = client.futures_mark_price(symbol=symbol)
        price = Decimal(ticker["markPrice"])
        logger.debug("Futures mark price {} = {}", symbol, price)
        return price
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_symbol_ticker failed for {}: {}", symbol, exc)
        raise


# ---------------------------------------------------------------------------
# Account / positions
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

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
