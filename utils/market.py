"""Public market data — no authentication required."""

import asyncio
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

import websockets
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from dateutil import parser as dateutil_parser
from loguru import logger

from utils.general import with_retry

_FSTREAM_URL = "wss://fstream.binance.com"
_FSTREAM_TESTNET_URL = "wss://stream.binancefuture.com"


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


_BINANCE_KLINE_LIMIT = 1500


def _parse_candles(raw: list) -> list[dict]:
    return [
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


def get_futures_ohlcv(
    client: Client,
    symbol: str,
    interval: str,
    limit: int = 100,
    start_str: Optional[str] = None,
    end_str: Optional[str] = None,
) -> list[dict]:
    """Fetch OHLCV candle data as a list of named dicts.

    Paginates automatically when limit > 1500 (Binance's per-request cap),
    walking backwards from the most recent candle.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        interval: Kline interval, e.g. "1m", "5m", "1h", "1d".
        limit: Max candles to return.
        start_str: Optional start time, e.g. "1 Jan 2024", "2 hours ago UTC".
        end_str: Optional end time in the same format as start_str.

    Returns:
        List of dicts with keys: open_time, open, high, low, close, volume,
        close_time. Prices and volume are Decimal.
    """
    try:
        if start_str is not None or end_str is not None:
            kwargs: dict = {"symbol": symbol, "interval": interval, "limit": min(limit, _BINANCE_KLINE_LIMIT)}
            if start_str is not None:
                kwargs["startTime"] = _to_ms(start_str)
            if end_str is not None:
                kwargs["endTime"] = _to_ms(end_str)
            raw = with_retry(lambda: client.futures_klines(**kwargs))
            candles = _parse_candles(raw)
            logger.debug("Fetched {} OHLCV candles for {} @ {}", len(candles), symbol, interval)
            return candles

        all_candles: list[dict] = []
        end_ms: Optional[int] = None
        remaining = limit
        num_requests = 0

        while remaining > 0:
            batch = min(remaining, _BINANCE_KLINE_LIMIT)
            end_ts = end_ms
            raw = with_retry(lambda: client.futures_klines(
                symbol=symbol, interval=interval, limit=batch,
                **({'endTime': end_ts} if end_ts is not None else {}),
            ))
            num_requests += 1
            if not raw:
                break
            all_candles = _parse_candles(raw) + all_candles
            remaining -= len(raw)
            if len(raw) < batch:
                break
            end_ms = raw[0][0] - 1

        logger.info(
            "Prefetched {} OHLCV candles for {} @ {} ({} request{})",
            len(all_candles), symbol, interval, num_requests, "s" if num_requests != 1 else "",
        )
        return all_candles[-limit:]

    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_futures_ohlcv failed for {}: {}", symbol, exc)
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


def _interval_ms(interval: str) -> int:
    n, unit = int(interval[:-1]), interval[-1]
    return {"m": n * 60_000, "h": n * 3_600_000, "d": n * 86_400_000}[unit]


class _KlineStreamManager:
    """Direct WebSocket connection to Binance Futures kline streams.

    Subscribes to many (symbol, interval) pairs on a single connection. On every closed
    candle, detects time gaps against the last-seen candle for the same (symbol, interval)
    and REST-fills missing candles before delivering the new one.
    """

    def __init__(
        self,
        client: Client,
        testnet: bool,
        pairs: list[tuple[str, str]],
        on_closed_candle: Callable[[str, str, dict], None],
    ) -> None:
        self._client = client
        self._testnet = testnet
        self._pairs = pairs
        self._on_closed_candle = on_closed_candle
        self._last_open_time: dict[tuple[str, str], int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="kline-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._stream_loop())
        except Exception as exc:
            if not self._stop.is_set():
                logger.error("Kline WS thread error: {}", exc)
        finally:
            # Cancel pending tasks (websockets keepalive, recv) so they don't
            # try to schedule callbacks on a closed loop after we close it.
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()

    async def _stream_loop(self) -> None:
        base = _FSTREAM_TESTNET_URL if self._testnet else _FSTREAM_URL
        streams = "/".join(f"{s.lower()}@kline_{i}" for s, i in self._pairs)
        url = f"{base}/stream?streams={streams}"

        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=60) as ws:
                    logger.debug("Kline WS connected: {}", url)
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        try:
                            self._handle_message(raw)
                        except Exception as exc:
                            logger.error("WS kline handler error: {}", exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._stop.is_set():
                    return
                logger.warning("Kline WS disconnected ({}), reconnecting in 5s", exc)
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    return

    def _handle_message(self, raw: str) -> None:
        wrapper = json.loads(raw)
        data = wrapper.get("data", wrapper)
        stream = wrapper.get("stream", "")
        if "@" not in stream:
            return
        symbol_part, kline_part = stream.split("@", 1)
        symbol = symbol_part.upper()
        interval = kline_part.split("_", 1)[1] if "_" in kline_part else ""
        candle = parse_kline_ws(data)
        if candle is None or not symbol or not interval:
            return
        self._deliver_with_gap_fill(symbol, interval, candle)

    def _deliver_with_gap_fill(self, symbol: str, interval: str, candle: dict) -> None:
        key = (symbol, interval)
        last = self._last_open_time.get(key)
        step = _interval_ms(interval)
        if last is not None and candle["open_time"] > last + step:
            missing_start = last + step
            missing_end = candle["open_time"] - 1
            try:
                filled = get_futures_ohlcv(
                    self._client, symbol, interval, limit=1500,
                    start_str=_ms_to_iso(missing_start),
                    end_str=_ms_to_iso(missing_end),
                )
                logger.warning("[ws] gap-fill {} {}: filled {} candle(s) between {} and {}",
                               symbol, interval, len(filled), missing_start, missing_end)
                for c in filled:
                    if c["open_time"] > last:
                        self._on_closed_candle(symbol, interval, c)
                        self._last_open_time[key] = c["open_time"]
            except Exception as exc:
                logger.error("[ws] gap-fill {} {} failed: {}", symbol, interval, exc)

        self._last_open_time[key] = candle["open_time"]
        self._on_closed_candle(symbol, interval, candle)


def _ms_to_iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def start_kline_streams(
    client: Client,
    testnet: bool,
    pairs: list[tuple[str, str]],
    on_closed_candle: Callable[[str, str, dict], None],
) -> _KlineStreamManager:
    """Subscribe to futures kline streams for every (symbol, interval) pair.

    Detects gaps after reconnect by comparing the new candle's open_time to the last
    one delivered for the same (symbol, interval); REST-fills any missing candles
    before delivering the new one. Reconnects automatically on disconnect.

    Args:
        client: Authenticated Binance client (used for REST gap-fills).
        testnet: If True, connects to the futures testnet WebSocket endpoint.
        pairs: List of (symbol, interval) tuples to subscribe to.
        on_closed_candle: Called with (symbol, interval, candle_dict) on every close.
            Invoked from a background thread — route shared state through a queue.

    Returns:
        The running _KlineStreamManager. Call .stop() on shutdown.
    """
    mgr = _KlineStreamManager(client, testnet, pairs, on_closed_candle)
    mgr.start()
    logger.info("Kline WS started ({} streams): {}", len(pairs), pairs)
    return mgr


def get_futures_best_bid_ask(client: Client, symbol: str) -> tuple[Decimal, Decimal]:
    """Return the current best bid and ask prices for a futures symbol.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".

    Returns:
        Tuple of (best_bid, best_ask) as Decimals.
    """
    try:
        ticker = client.futures_orderbook_ticker(symbol=symbol)
        bid = Decimal(ticker["bidPrice"])
        ask = Decimal(ticker["askPrice"])
        logger.debug("Futures best bid/ask {} = {}/{}", symbol, bid, ask)
        return bid, ask
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_futures_best_bid_ask failed for {}: {}", symbol, exc)
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
