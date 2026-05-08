import os
import time
from decimal import Decimal

import pytest

from utils import market

pytestmark = pytest.mark.integration


def test_get_futures_mark_price(client, symbol):
    price = market.get_futures_mark_price(client, symbol)
    assert isinstance(price, Decimal)
    assert price > 0


def test_get_futures_ohlcv(client, symbol):
    candles = market.get_futures_ohlcv(client, symbol, "5m", limit=10)
    assert len(candles) == 10
    c = candles[0]
    assert {"open_time", "open", "high", "low", "close", "volume", "close_time"} <= c.keys()
    assert c["high"] >= c["low"]
    assert isinstance(c["close"], Decimal)


def test_get_futures_ohlcv_with_start_str(client, symbol):
    candles = market.get_futures_ohlcv(client, symbol, "1h", limit=3, start_str="3 hours ago UTC")
    assert 1 <= len(candles) <= 3
    assert all("close" in c for c in candles)


def test_get_futures_ohlcv_pagination(client, symbol):
    """Verify pagination kicks in when limit exceeds Binance's 1500-candle cap."""
    candles = market.get_futures_ohlcv(client, symbol, "1m", limit=1600)
    assert len(candles) == 1600
    # Chronological order
    assert candles[0]["open_time"] < candles[-1]["open_time"]
    # No gaps — consecutive 1m open_times are exactly 60 000 ms apart
    for i in range(1, len(candles)):
        assert candles[i]["open_time"] == candles[i - 1]["open_time"] + 60_000


def test_parse_kline_ws_closed_candle():
    msg = {
        "e": "kline",
        "k": {
            "t": 1700000000000,
            "T": 1700000299999,
            "o": "30000.00",
            "h": "30100.00",
            "l": "29900.00",
            "c": "30050.00",
            "v": "12.345",
            "x": True,
        },
    }
    candle = market.parse_kline_ws(msg)
    assert candle is not None
    assert candle["open_time"] == 1700000000000
    assert candle["close"] == Decimal("30050.00")
    assert candle["high"] >= candle["low"]
    assert "is_closed" not in candle


def test_parse_kline_ws_open_candle_returns_none():
    msg = {
        "e": "kline",
        "k": {
            "t": 1700000000000, "T": 1700000299999,
            "o": "30000.00", "h": "30100.00", "l": "29900.00",
            "c": "30050.00", "v": "12.345", "x": False,
        },
    }
    assert market.parse_kline_ws(msg) is None


def test_parse_kline_ws_non_kline_event_returns_none():
    assert market.parse_kline_ws({"e": "trade", "p": "30000.00"}) is None


def test_start_kline_streams_connects(symbol):
    received: list = []

    twm = market.start_kline_streams(
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
        testnet=True,
        symbols=[symbol],
        interval="1m",
        on_closed_candle=lambda sym, candle: received.append((sym, candle)),
    )
    try:
        time.sleep(3)
        assert twm.is_alive(), "ThreadedWebsocketManager thread should be alive after connecting"
    finally:
        twm.stop()
