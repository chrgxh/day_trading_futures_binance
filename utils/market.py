"""Public market data — no authentication required."""

from decimal import Decimal
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from binance.helpers import date_to_milliseconds
from loguru import logger


def get_futures_ohlcv(
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
        limit: Max candles to return (1–1000).
        start_str: Optional start time, e.g. "1 Jan 2024", "2 hours ago UTC".
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
        raw = client.futures_klines(**kwargs)
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


def get_futures_mark_price(client: Client, symbol: str) -> Decimal:
    """Return the latest futures mark price for a symbol.

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
