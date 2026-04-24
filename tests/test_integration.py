"""Integration tests — hit the Binance testnet directly.

These tests require real testnet credentials in .env and an active network
connection. They are marked with `integration` so they can be run explicitly:

    pytest tests/test_integration.py -v -m integration

and excluded from the default suite:

    pytest -m "not integration"
"""

import os
from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import load_dotenv

import utils

load_dotenv(Path(__file__).parent.parent / ".env")


@pytest.fixture(scope="module")
def client():
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    testnet = os.environ.get("BINANCE_TESTNET", "true").lower() == "true"
    if not api_key or not api_secret:
        pytest.skip("BINANCE_API_KEY / BINANCE_API_SECRET not set in .env")
    return utils.build_client(api_key, api_secret, testnet=testnet)


pytestmark = pytest.mark.integration


def test_connection(client):
    assert utils.check_connection(client) is True


def test_btcusdt_price_is_positive(client):
    price = utils.get_symbol_ticker(client, "BTCUSDT")
    assert isinstance(price, Decimal)
    assert price > 0


def test_ohlcv_btcusdt_returns_correct_count(client):
    candles = utils.get_ohlcv(client, "BTCUSDT", "1h", limit=10)
    assert len(candles) == 10


def test_ohlcv_candle_structure_is_valid(client):
    candles = utils.get_ohlcv(client, "BTCUSDT", "1h", limit=1)
    assert len(candles) == 1
    c = candles[0]
    assert c["high"] >= c["low"]
    assert c["high"] >= c["open"]
    assert c["high"] >= c["close"]
    assert c["low"] <= c["open"]
    assert c["low"] <= c["close"]
    assert c["volume"] >= 0
    assert c["close_time"] > c["open_time"]


def test_ohlcv_with_start_str(client):
    candles = utils.get_ohlcv(client, "BTCUSDT", "1d", limit=5, start_str="1 Jan 2024")
    assert len(candles) > 0
    assert candles[0]["close"] > 0


def test_open_positions_returns_list(client):
    positions = utils.get_open_positions(client)
    assert isinstance(positions, list)


def test_open_positions_structure(client):
    positions = utils.get_open_positions(client)
    for p in positions:
        assert p["side"] in ("LONG", "SHORT")
        assert p["amount"] != 0
        assert p["entry_price"] > 0
        assert isinstance(p["leverage"], int)
