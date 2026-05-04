from decimal import Decimal

import pytest

from utils import account

pytestmark = pytest.mark.integration


def test_connection(client):
    assert account.check_futures_connection(client) is True


def test_get_futures_balance(client):
    balances = account.get_futures_balance(client)
    assert isinstance(balances, list)
    assert len(balances) > 0
    b = balances[0]
    assert {"asset", "balance", "available", "unrealized_pnl"} <= b.keys()
    assert isinstance(b["balance"], Decimal)


def test_get_futures_positions_returns_list(client, symbol):
    result = account.get_futures_positions(client, symbol)
    assert isinstance(result, list)


def test_get_symbol_info(client, symbol, sym_info):
    expected_keys = {"symbol", "tick_size", "step_size", "min_qty", "max_qty", "min_notional", "price_precision", "qty_precision"}
    assert expected_keys <= sym_info.keys()
    assert sym_info["symbol"] == symbol
    assert sym_info["tick_size"] > 0
    assert sym_info["step_size"] > 0


def test_set_leverage(client, symbol):
    account.set_leverage(client, symbol, 5)
