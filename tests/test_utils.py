"""Unit tests for utils.py — all Binance client calls are mocked."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from binance.exceptions import BinanceAPIException, BinanceRequestException

from utils import check_connection, get_ohlcv, get_open_positions, get_symbol_ticker


def _mock_client() -> MagicMock:
    return MagicMock()


def _api_exc(code: int = -1, msg: str = "error", status: int = 400) -> BinanceAPIException:
    return BinanceAPIException(MagicMock(status_code=status), status, f'{{"code": {code}, "msg": "{msg}"}}')


def _make_kline(close: float) -> list:
    return [0, "100.0", "110.0", "90.0", str(close), "500.0", 60000, "0", 0, "0", "0", "0"]


# ---------------------------------------------------------------------------
# check_connection
# ---------------------------------------------------------------------------

class TestCheckConnection:
    def test_returns_true_on_success(self):
        client = _mock_client()
        client.get_server_time.return_value = {"serverTime": 1700000000000}
        assert check_connection(client) is True
        client.ping.assert_called_once()
        client.get_server_time.assert_called_once()

    def test_returns_false_on_api_exception(self):
        client = _mock_client()
        client.ping.side_effect = _api_exc()
        assert check_connection(client) is False

    def test_returns_false_on_request_exception(self):
        client = _mock_client()
        client.ping.side_effect = BinanceRequestException("network error")
        assert check_connection(client) is False


# ---------------------------------------------------------------------------
# get_open_positions
# ---------------------------------------------------------------------------

def _make_futures_position(symbol: str, amt: str, entry: str = "45000.0") -> dict:
    return {
        "symbol": symbol,
        "positionAmt": amt,
        "entryPrice": entry,
        "markPrice": "46000.0",
        "unRealizedProfit": "1.0",
        "liquidationPrice": "40000.0",
        "leverage": "10",
        "positionSide": "BOTH",
    }


class TestGetOpenPositions:
    def test_filters_zero_amount_positions(self):
        client = _mock_client()
        client.futures_position_information.return_value = [
            _make_futures_position("BTCUSDT", "0.001"),
            _make_futures_position("ETHUSDT", "0.0"),   # closed
            _make_futures_position("BNBUSDT", "-1.0"),  # short
        ]
        result = get_open_positions(client)
        symbols = {p["symbol"] for p in result}
        assert symbols == {"BTCUSDT", "BNBUSDT"}

    def test_long_side_for_positive_amount(self):
        client = _mock_client()
        client.futures_position_information.return_value = [
            _make_futures_position("BTCUSDT", "0.5"),
        ]
        result = get_open_positions(client)
        assert result[0]["side"] == "LONG"
        assert result[0]["amount"] == Decimal("0.5")

    def test_short_side_for_negative_amount(self):
        client = _mock_client()
        client.futures_position_information.return_value = [
            _make_futures_position("BTCUSDT", "-0.5"),
        ]
        result = get_open_positions(client)
        assert result[0]["side"] == "SHORT"

    def test_returns_expected_keys(self):
        client = _mock_client()
        client.futures_position_information.return_value = [
            _make_futures_position("BTCUSDT", "0.1"),
        ]
        result = get_open_positions(client)
        assert set(result[0].keys()) == {
            "symbol", "side", "amount", "entry_price",
            "mark_price", "unrealized_pnl", "leverage", "liquidation_price",
        }

    def test_symbol_filter_passed_to_client(self):
        client = _mock_client()
        client.futures_position_information.return_value = [
            _make_futures_position("BTCUSDT", "0.1"),
        ]
        get_open_positions(client, symbol="BTCUSDT")
        client.futures_position_information.assert_called_once_with(symbol="BTCUSDT")

    def test_no_symbol_calls_without_kwargs(self):
        client = _mock_client()
        client.futures_position_information.return_value = []
        get_open_positions(client)
        client.futures_position_information.assert_called_once_with()

    def test_raises_on_api_exception(self):
        client = _mock_client()
        client.futures_position_information.side_effect = _api_exc(code=-2015, status=401)
        with pytest.raises(BinanceAPIException):
            get_open_positions(client)


# ---------------------------------------------------------------------------
# get_ohlcv
# ---------------------------------------------------------------------------

class TestGetOhlcv:
    def test_returns_named_dict_keys(self):
        client = _mock_client()
        client.get_klines.return_value = [_make_kline(50000.0)]
        result = get_ohlcv(client, "BTCUSDT", "1h")
        assert len(result) == 1
        assert set(result[0].keys()) == {"open_time", "open", "high", "low", "close", "volume", "close_time"}

    def test_prices_are_decimal(self):
        client = _mock_client()
        client.get_klines.return_value = [_make_kline(50000.5)]
        result = get_ohlcv(client, "BTCUSDT", "1h")
        candle = result[0]
        for key in ("open", "high", "low", "close", "volume"):
            assert isinstance(candle[key], Decimal), f"{key} should be Decimal"

    def test_close_value_matches_input(self):
        client = _mock_client()
        client.get_klines.return_value = [_make_kline(42123.99)]
        result = get_ohlcv(client, "BTCUSDT", "1h")
        assert result[0]["close"] == Decimal("42123.99")

    def test_passes_limit_to_client(self):
        client = _mock_client()
        client.get_klines.return_value = []
        get_ohlcv(client, "BTCUSDT", "5m", limit=50)
        assert client.get_klines.call_args.kwargs["limit"] == 50

    def test_start_str_adds_start_time_kwarg(self):
        client = _mock_client()
        client.get_klines.return_value = []
        get_ohlcv(client, "BTCUSDT", "1h", start_str="1 Jan 2024")
        kwargs = client.get_klines.call_args.kwargs
        assert "startTime" in kwargs
        assert isinstance(kwargs["startTime"], int)

    def test_end_str_adds_end_time_kwarg(self):
        client = _mock_client()
        client.get_klines.return_value = []
        get_ohlcv(client, "BTCUSDT", "1h", end_str="1 Feb 2024")
        kwargs = client.get_klines.call_args.kwargs
        assert "endTime" in kwargs

    def test_no_time_args_omits_start_end_time(self):
        client = _mock_client()
        client.get_klines.return_value = []
        get_ohlcv(client, "BTCUSDT", "1h")
        kwargs = client.get_klines.call_args.kwargs
        assert "startTime" not in kwargs
        assert "endTime" not in kwargs

    def test_raises_on_invalid_symbol(self):
        client = _mock_client()
        client.get_klines.side_effect = _api_exc(code=-1121, msg="Invalid symbol.")
        with pytest.raises(BinanceAPIException):
            get_ohlcv(client, "INVALIDSYMBOL", "1h")
