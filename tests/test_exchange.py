"""Unit tests for exchange.py — all Binance client calls are mocked."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from binance.exceptions import BinanceAPIException, BinanceRequestException

from utils.exchange import check_connection, get_open_positions


def _mock_client() -> MagicMock:
    return MagicMock()


def _api_exc(code: int = -1, msg: str = "error", status: int = 400) -> BinanceAPIException:
    return BinanceAPIException(MagicMock(status_code=status), status, f'{{"code": {code}, "msg": "{msg}"}}')


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

class TestGetOpenPositions:
    def test_filters_zero_amount_positions(self):
        client = _mock_client()
        client.futures_position_information.return_value = [
            _make_futures_position("BTCUSDT", "0.001"),
            _make_futures_position("ETHUSDT", "0.0"),
            _make_futures_position("BNBUSDT", "-1.0"),
        ]
        result = get_open_positions(client)
        assert {p["symbol"] for p in result} == {"BTCUSDT", "BNBUSDT"}

    def test_long_side_for_positive_amount(self):
        client = _mock_client()
        client.futures_position_information.return_value = [_make_futures_position("BTCUSDT", "0.5")]
        result = get_open_positions(client)
        assert result[0]["side"] == "LONG"
        assert result[0]["amount"] == Decimal("0.5")

    def test_short_side_for_negative_amount(self):
        client = _mock_client()
        client.futures_position_information.return_value = [_make_futures_position("BTCUSDT", "-0.5")]
        assert get_open_positions(client)[0]["side"] == "SHORT"

    def test_returns_expected_keys(self):
        client = _mock_client()
        client.futures_position_information.return_value = [_make_futures_position("BTCUSDT", "0.1")]
        result = get_open_positions(client)
        assert set(result[0].keys()) == {
            "symbol", "side", "amount", "entry_price",
            "mark_price", "unrealized_pnl", "leverage", "liquidation_price",
        }

    def test_symbol_filter_passed_to_client(self):
        client = _mock_client()
        client.futures_position_information.return_value = [_make_futures_position("BTCUSDT", "0.1")]
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
