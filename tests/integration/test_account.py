import time
from decimal import Decimal

import pytest

from utils import account, orders as orders_mod, positions as positions_mod

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


def test_get_futures_recent_trades_returns_trades_after_open_and_close(client, symbol, sym_info):
    """Opening and immediately closing a position produces closing-side trades with realized_pnl."""
    start_ms = int(time.time() * 1000)

    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])
    orders_mod.cancel_all_orders(client, symbol)
    positions_mod.close_position(client, symbol)
    orders_mod.place_market_order(client, symbol, "BUY", qty)
    positions_mod.close_position(client, symbol)

    trades = account.get_futures_recent_trades(client, symbol, start_time_ms=start_ms)

    assert isinstance(trades, list)
    assert len(trades) > 0

    expected_keys = {"trade_id", "order_id", "side", "price", "qty", "realized_pnl",
                     "commission", "commission_asset", "time", "is_maker"}
    for t in trades:
        assert expected_keys <= t.keys()
        assert isinstance(t["realized_pnl"], Decimal)
        assert isinstance(t["price"], Decimal)
        assert isinstance(t["qty"], Decimal)

    closing_trades = [t for t in trades if t["side"] == "SELL"]
    assert len(closing_trades) > 0


def test_get_futures_recent_trades_empty_when_start_is_now(client, symbol):
    """start_time_ms of the current millisecond returns no trades (none can have occurred yet)."""
    now_ms = int(time.time() * 1000)
    trades = account.get_futures_recent_trades(client, symbol, start_time_ms=now_ms)
    assert trades == []
