"""Unit tests for market.py — all Binance client calls are mocked."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from binance.exceptions import BinanceAPIException

from utils.market import get_ohlcv


def _mock_client() -> MagicMock:
    return MagicMock()


def _api_exc(code: int = -1, msg: str = "error", status: int = 400) -> BinanceAPIException:
    return BinanceAPIException(MagicMock(status_code=status), status, f'{{"code": {code}, "msg": "{msg}"}}')


def _make_kline(close: float) -> list:
    return [0, "100.0", "110.0", "90.0", str(close), "500.0", 60000, "0", 0, "0", "0", "0"]


class TestGetOhlcv:
    def test_returns_named_dict_keys(self):
        client = _mock_client()
        client.futures_klines.return_value = [_make_kline(50000.0)]
        result = get_ohlcv(client, "BTCUSDT", "1h")
        assert set(result[0].keys()) == {"open_time", "open", "high", "low", "close", "volume", "close_time"}

    def test_prices_are_decimal(self):
        client = _mock_client()
        client.futures_klines.return_value = [_make_kline(50000.5)]
        candle = get_ohlcv(client, "BTCUSDT", "1h")[0]
        for key in ("open", "high", "low", "close", "volume"):
            assert isinstance(candle[key], Decimal)

    def test_close_value_matches_input(self):
        client = _mock_client()
        client.futures_klines.return_value = [_make_kline(42123.99)]
        assert get_ohlcv(client, "BTCUSDT", "1h")[0]["close"] == Decimal("42123.99")

    def test_passes_limit_to_client(self):
        client = _mock_client()
        client.futures_klines.return_value = []
        get_ohlcv(client, "BTCUSDT", "5m", limit=50)
        assert client.futures_klines.call_args.kwargs["limit"] == 50

    def test_start_str_adds_start_time_kwarg(self):
        client = _mock_client()
        client.futures_klines.return_value = []
        get_ohlcv(client, "BTCUSDT", "1h", start_str="1 Jan 2024")
        kwargs = client.futures_klines.call_args.kwargs
        assert "startTime" in kwargs
        assert isinstance(kwargs["startTime"], int)

    def test_end_str_adds_end_time_kwarg(self):
        client = _mock_client()
        client.futures_klines.return_value = []
        get_ohlcv(client, "BTCUSDT", "1h", end_str="1 Feb 2024")
        assert "endTime" in client.futures_klines.call_args.kwargs

    def test_no_time_args_omits_start_end_time(self):
        client = _mock_client()
        client.futures_klines.return_value = []
        get_ohlcv(client, "BTCUSDT", "1h")
        kwargs = client.futures_klines.call_args.kwargs
        assert "startTime" not in kwargs
        assert "endTime" not in kwargs

    def test_raises_on_invalid_symbol(self):
        client = _mock_client()
        client.futures_klines.side_effect = _api_exc(code=-1121, msg="Invalid symbol.")
        with pytest.raises(BinanceAPIException):
            get_ohlcv(client, "INVALIDSYMBOL", "1h")
