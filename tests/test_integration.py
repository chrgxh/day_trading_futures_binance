"""Integration tests — hit the Binance testnet directly.

Run with: pytest tests/test_integration.py -v -m integration
Exclude from default suite: pytest -m "not integration"
"""

import os
from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import load_dotenv

from utils import exchange, market
from utils.exchange import get_futures_balance

load_dotenv(Path(__file__).parent.parent / ".env")


@pytest.fixture(scope="module")
def client():
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    testnet = os.environ.get("BINANCE_TESTNET", "true").lower() == "true"
    if not api_key or not api_secret:
        pytest.skip("BINANCE_API_KEY / BINANCE_API_SECRET not set in .env")
    return exchange.build_client(api_key, api_secret, testnet=testnet)


pytestmark = pytest.mark.integration


def test_futures_connection(client):
    assert exchange.check_futures_connection(client) is True


def test_futures_mark_price_is_positive(client):
    price = market.get_futures_mark_price(client, "BTCUSDT")
    assert isinstance(price, Decimal)
    assert price > 0


def test_futures_ohlcv_returns_correct_count(client):
    candles = market.get_futures_ohlcv(client, "BTCUSDT", "1h", limit=10)
    assert len(candles) == 10


def test_futures_ohlcv_candle_structure_is_valid(client):
    candles = market.get_futures_ohlcv(client, "BTCUSDT", "1h", limit=1)
    c = candles[0]
    assert c["high"] >= c["low"]
    assert c["high"] >= c["open"]
    assert c["high"] >= c["close"]
    assert c["low"] <= c["open"]
    assert c["low"] <= c["close"]
    assert c["volume"] >= 0
    assert c["close_time"] > c["open_time"]


def test_futures_ohlcv_with_start_str(client):
    candles = market.get_futures_ohlcv(client, "BTCUSDT", "1d", limit=5, start_str="1 Jan 2024")
    assert len(candles) > 0
    assert candles[0]["close"] > 0


def test_futures_balance_returns_list(client):
    balances = get_futures_balance(client)
    assert isinstance(balances, list)


def test_futures_balance_structure(client):
    balances = get_futures_balance(client)
    for b in balances:
        assert set(b.keys()) == {"asset", "balance", "available", "unrealized_pnl"}
        assert b["balance"] > 0


def test_futures_positions_returns_list(client):
    positions = exchange.get_futures_positions(client)
    assert isinstance(positions, list)


def test_futures_positions_structure(client):
    positions = exchange.get_futures_positions(client)
    for p in positions:
        assert p["side"] in ("LONG", "SHORT")
        assert p["amount"] != 0
        assert p["entry_price"] > 0
        assert isinstance(p["leverage"], int)
