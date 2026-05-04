from decimal import Decimal

import pytest

from utils import algo_orders, orders

pytestmark = pytest.mark.integration


def test_stop_market_order(client, symbol, sym_info, open_position):
    stop = (open_position["entry_price"] * Decimal("0.95")).quantize(sym_info["tick_size"])
    qty = abs(open_position["amount"])

    order = algo_orders.place_stop_market_order(client, symbol, "SELL", qty, stop)
    assert order["is_algo"] is True
    assert order["status"] == "WORKING"
    assert any(o["order_id"] == order["order_id"] for o in orders.get_open_orders(client, symbol))

    algo_orders.cancel_algo_order(client, symbol, order["order_id"])


def test_take_profit_market_order(client, symbol, sym_info, open_position):
    tp = (open_position["entry_price"] * Decimal("1.05")).quantize(sym_info["tick_size"])
    qty = abs(open_position["amount"])

    order = algo_orders.place_take_profit_market_order(client, symbol, "SELL", qty, tp)
    assert order["is_algo"] is True
    assert order["status"] == "WORKING"

    algo_orders.cancel_algo_order(client, symbol, order["order_id"])


def test_stop_limit_order(client, symbol, sym_info, open_position):
    tick = sym_info["tick_size"]
    stop = (open_position["entry_price"] * Decimal("0.95")).quantize(tick)
    limit = (open_position["entry_price"] * Decimal("0.949")).quantize(tick)
    qty = abs(open_position["amount"])

    order = algo_orders.place_stop_limit_order(client, symbol, "SELL", qty, stop, limit)
    assert order["is_algo"] is True
    assert order["stop_price"] == stop

    algo_orders.cancel_algo_order(client, symbol, order["order_id"])


def test_take_profit_limit_order(client, symbol, sym_info, open_position):
    tick = sym_info["tick_size"]
    tp = (open_position["entry_price"] * Decimal("1.05")).quantize(tick)
    limit = (open_position["entry_price"] * Decimal("1.051")).quantize(tick)
    qty = abs(open_position["amount"])

    order = algo_orders.place_take_profit_limit_order(client, symbol, "SELL", qty, tp, limit)
    assert order["is_algo"] is True
    assert order["stop_price"] == tp

    algo_orders.cancel_algo_order(client, symbol, order["order_id"])
