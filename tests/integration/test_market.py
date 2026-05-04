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
