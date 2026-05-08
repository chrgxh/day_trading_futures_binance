"""Public market data — no authentication required."""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from binance import ThreadedWebsocketManager
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from dateutil import parser as dateutil_parser
from loguru import logger


def _to_ms(date_str: str) -> int:
    """Convert a date string to milliseconds since UTC epoch.

    Supports ISO dates ("1 Jan 2024", "2024-01-01") and relative strings
    ("2 hours ago UTC", "1 day ago UTC").
    """
    match = re.match(r"(\d+)\s+(minute|hour|day|week)s?\s+ago", date_str, re.IGNORECASE)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        delta = {"minute": timedelta(minutes=amount), "hour": timedelta(hours=amount),
                 "day": timedelta(days=amount), "week": timedelta(weeks=amount)}[unit]
        dt = datetime.now(timezone.utc) - delta
    else:
        dt = dateutil_parser.parse(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return int((dt - epoch).total_seconds() * 1000)


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
            kwargs["startTime"] = _to_ms(start_str)
        if end_str is not None:
            kwargs["endTime"] = _to_ms(end_str)
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


def parse_kline_ws(msg: dict) -> dict | None:
    """Parse a Binance WebSocket kline event and return the candle dict if the candle is closed.

    Args:
        msg: Raw message dict from a kline WebSocket stream.

    Returns:
        Candle dict (open_time, open, high, low, close, volume, close_time) if the
        candle is closed, otherwise None. Prices and volume are Decimal.
    """
    if msg.get("e") != "kline" or not msg["k"]["x"]:
        return None
    k = msg["k"]
    return {
        "open_time": k["t"],
        "open": Decimal(k["o"]),
        "high": Decimal(k["h"]),
        "low": Decimal(k["l"]),
        "close": Decimal(k["c"]),
        "volume": Decimal(k["v"]),
        "close_time": k["T"],
    }


def start_kline_streams(
    api_key: str,
    api_secret: str,
    testnet: bool,
    symbols: list[str],
    interval: str,
    on_closed_candle: Callable[[str, dict], None],
) -> ThreadedWebsocketManager:
    """Subscribe to futures kline streams for each symbol and call on_closed_candle on every close.

    The callback is invoked from a background thread — callers must ensure any shared state
    they access inside the callback is thread-safe (e.g. by routing through a queue).

    Args:
        api_key: Binance API key.
        api_secret: Binance API secret.
        testnet: If True, connects to the futures testnet WebSocket endpoint.
        symbols: List of trading pairs to subscribe to.
        interval: Kline interval, e.g. "5m".
        on_closed_candle: Called with (symbol, candle_dict) whenever a candle closes.

    Returns:
        The running ThreadedWebsocketManager. Call .stop() on shutdown.
    """
    def make_callback(sym: str) -> Callable[[dict], None]:
        def handle(msg: dict) -> None:
            try:
                candle = parse_kline_ws(msg)
                if candle is not None:
                    on_closed_candle(sym, candle)
            except Exception as exc:
                logger.error("WS kline handler error for {}: {}", sym, exc)
        return handle

    twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret, testnet=testnet)
    twm.start()
    for symbol in symbols:
        twm.start_kline_futures_socket(callback=make_callback(symbol), symbol=symbol, interval=interval)
    logger.info("Kline WebSocket streams started for {} @ {}", symbols, interval)
    return twm


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
