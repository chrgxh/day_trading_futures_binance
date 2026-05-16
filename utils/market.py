"""Public market data — no authentication required."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from binance import ThreadedWebsocketManager
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from dateutil import parser as dateutil_parser
from loguru import logger

from utils.general import with_retry


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


def drop_forming_candle(candles: list[dict]) -> list[dict]:
    """Drop a trailing still-forming candle, if present.

    Binance REST klines always include the currently-forming candle as the
    last element. Warmup must exclude it: the kline WebSocket delivers only
    *closed* candles (``parse_kline_ws`` gates on ``k["x"]``), so a forming
    candle seeded into a strategy buffer would never be refreshed until it
    finally closes — leaving the strategy evaluating a stale, partial candle
    for up to one full interval after startup.

    Args:
        candles: OHLCV candle dicts as returned by ``get_futures_ohlcv``.

    Returns:
        The list without its trailing candle if that candle has not closed yet
        (``close_time`` at or after the current time); otherwise unchanged.
    """
    if not candles:
        return candles
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if candles[-1]["close_time"] >= now_ms:
        return candles[:-1]
    return candles


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
    """Binance Futures kline streams via the python-binance socket manager.

    Subscribes to many (symbol, interval) pairs on a single multiplexed connection
    through `ThreadedWebsocketManager`, which owns the socket URL (testnet vs
    mainnet), its own thread + event loop, and the reconnect logic. On every closed
    candle, detects time gaps against the last-seen candle for the same
    (symbol, interval) and REST-fills missing candles before delivering the new one.
    """

    def __init__(
        self,
        client: Client,
        testnet: bool,
        pairs: list[tuple[str, str]],
        on_closed_candle: Callable[[str, str, dict], None],
    ) -> None:
        self._client = client
        self._pairs = pairs
        self._on_closed_candle = on_closed_candle
        self._last_open_time: dict[tuple[str, str], int] = {}
        # A dedicated loop per manager: ThreadedWebsocketManager otherwise
        # defaults to asyncio.get_event_loop(), which hands every manager
        # constructed on the main thread the *same* loop — the second one to
        # start then fails ("Socket Manager failed to initialize").
        self._loop = asyncio.new_event_loop()
        self._twm = ThreadedWebsocketManager(
            api_key=client.API_KEY,
            api_secret=client.API_SECRET,
            testnet=testnet,
            loop=self._loop,
        )
        # ThreadedWebsocketManager is a non-daemon Thread and stop() only sets
        # flags — it never joins. A stuck SDK socket teardown would otherwise
        # block interpreter exit on shutdown; the stream carries no
        # shutdown-critical state, so mark it daemon.
        self._twm.daemon = True

    def start(self) -> None:
        self._twm.start()
        # The socket's ReconnectingWebsocket read loop is scheduled on whatever
        # asyncio.get_event_loop() returns on *this* thread at construction time
        # — not the loop handed to the manager. Make the manager's loop current
        # here so the read loop lands on the loop the manager actually runs;
        # otherwise it never executes and no candles are ever delivered.
        asyncio.set_event_loop(self._loop)
        streams = [f"{s.lower()}@kline_{i}" for s, i in self._pairs]
        self._twm.start_futures_multiplex_socket(
            callback=self._handle_message, streams=streams,
        )

    def stop(self) -> None:
        try:
            self._twm.stop()
        except Exception as exc:
            logger.warning("Kline WS stop failed: {}", exc)

    def _handle_message(self, msg: dict) -> None:
        """Handle one multiplexed WS message (a dict from ThreadedWebsocketManager).

        Combined-stream payloads are wrapped as {"stream": ..., "data": ...}; the
        socket manager also emits {"e": "error", ...} dicts on disconnect (it then
        reconnects on its own — the gap-fill covers any candles missed meanwhile).
        """
        try:
            if msg.get("e") == "error":
                logger.warning("[ws] kline stream error: {}", msg.get("m"))
                return
            data = msg.get("data", msg)
            stream = msg.get("stream", "")
            if "@" not in stream:
                return
            symbol_part, kline_part = stream.split("@", 1)
            symbol = symbol_part.upper()
            interval = kline_part.split("_", 1)[1] if "_" in kline_part else ""
            candle = parse_kline_ws(data)
            if candle is None or not symbol or not interval:
                return
            self._deliver_with_gap_fill(symbol, interval, candle)
        except Exception as exc:
            logger.error("WS kline handler error: {}", exc)

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
