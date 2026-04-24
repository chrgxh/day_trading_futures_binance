"""Unit tests for exchange.py — all Binance client calls are mocked."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from binance.exceptions import BinanceAPIException, BinanceRequestException

from utils.exchange import check_futures_connection, get_futures_balance, get_futures_positions


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
# check_futures_connection
# ---------------------------------------------------------------------------

class TestCheckFuturesConnection:
    def test_returns_true_on_success(self):
        client = _mock_client()
        client.futures_time.return_value = {"serverTime": 1700000000000}
        assert check_futures_connection(client) is True
        client.futures_ping.assert_called_once()
        client.futures_time.assert_called_once()

    def test_returns_false_on_api_exception(self):
        client = _mock_client()
        client.futures_ping.side_effect = _api_exc()
        assert check_futures_connection(client) is False

    def test_returns_false_on_request_exception(self):
        client = _mock_client()
        client.futures_ping.side_effect = BinanceRequestException("network error")
        assert check_futures_connection(client) is False


# ---------------------------------------------------------------------------
# get_futures_balance
# ---------------------------------------------------------------------------

class TestGetFuturesBalance:
    def _make_balance(self, asset: str, balance: str, available: str = None, pnl: str = "0.0") -> dict:
        return {
            "asset": asset,
            "balance": balance,
            "availableBalance": available or balance,
            "crossUnPnl": pnl,
        }

    def test_filters_zero_balance_assets(self):
        client = _mock_client()
        client.futures_account_balance.return_value = [
            self._make_balance("USDT", "1000.0"),
            self._make_balance("BTC", "0.0"),
        ]
        result = get_futures_balance(client)
        assert len(result) == 1
        assert result[0]["asset"] == "USDT"

    def test_returns_expected_keys(self):
        client = _mock_client()
        client.futures_account_balance.return_value = [self._make_balance("USDT", "500.0")]
        result = get_futures_balance(client)
        assert set(result[0].keys()) == {"asset", "balance", "available", "unrealized_pnl"}

    def test_values_are_decimal(self):
        client = _mock_client()
        client.futures_account_balance.return_value = [
            self._make_balance("USDT", "1000.50", available="800.25", pnl="-12.5"),
        ]
        result = get_futures_balance(client)
        assert result[0]["balance"] == Decimal("1000.50")
        assert result[0]["available"] == Decimal("800.25")
        assert result[0]["unrealized_pnl"] == Decimal("-12.5")

    def test_raises_on_api_exception(self):
        client = _mock_client()
        client.futures_account_balance.side_effect = _api_exc(code=-2015, status=401)
        with pytest.raises(BinanceAPIException):
            get_futures_balance(client)


# ---------------------------------------------------------------------------
# get_futures_positions
# ---------------------------------------------------------------------------

class TestGetFuturesPositions:
    def test_filters_zero_amount_positions(self):
        client = _mock_client()
        client.futures_position_information.return_value = [
            _make_futures_position("BTCUSDT", "0.001"),
            _make_futures_position("ETHUSDT", "0.0"),
            _make_futures_position("BNBUSDT", "-1.0"),
        ]
        result = get_futures_positions(client)
        assert {p["symbol"] for p in result} == {"BTCUSDT", "BNBUSDT"}

    def test_long_side_for_positive_amount(self):
        client = _mock_client()
        client.futures_position_information.return_value = [_make_futures_position("BTCUSDT", "0.5")]
        result = get_futures_positions(client)
        assert result[0]["side"] == "LONG"
        assert result[0]["amount"] == Decimal("0.5")

    def test_short_side_for_negative_amount(self):
        client = _mock_client()
        client.futures_position_information.return_value = [_make_futures_position("BTCUSDT", "-0.5")]
        assert get_futures_positions(client)[0]["side"] == "SHORT"

    def test_returns_expected_keys(self):
        client = _mock_client()
        client.futures_position_information.return_value = [_make_futures_position("BTCUSDT", "0.1")]
        result = get_futures_positions(client)
        assert set(result[0].keys()) == {
            "symbol", "side", "amount", "entry_price",
            "mark_price", "unrealized_pnl", "leverage", "liquidation_price",
        }

    def test_symbol_filter_passed_to_client(self):
        client = _mock_client()
        client.futures_position_information.return_value = [_make_futures_position("BTCUSDT", "0.1")]
        get_futures_positions(client, symbol="BTCUSDT")
        client.futures_position_information.assert_called_once_with(symbol="BTCUSDT")

    def test_no_symbol_calls_without_kwargs(self):
        client = _mock_client()
        client.futures_position_information.return_value = []
        get_futures_positions(client)
        client.futures_position_information.assert_called_once_with()

    def test_raises_on_api_exception(self):
        client = _mock_client()
        client.futures_position_information.side_effect = _api_exc(code=-2015, status=401)
        with pytest.raises(BinanceAPIException):
            get_futures_positions(client)
