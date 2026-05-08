from decimal import Decimal

import pytest

from utils import market, orders
from utils import algo_orders

pytestmark = pytest.mark.integration


def test_get_open_orders_empty(client, symbol, clean_orders):
    assert orders.get_open_orders(client, symbol) == []


def test_place_limit_order_appears_in_open_orders(client, symbol, sym_info, clean_orders):
    mark = market.get_futures_mark_price(client, symbol)
    price = (mark * Decimal("0.85")).quantize(sym_info["tick_size"])
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])

    order = orders.place_limit_order(client, symbol, "BUY", qty, price)
    assert order["is_algo"] is False

    open_orders = orders.get_open_orders(client, symbol)
    assert any(o["order_id"] == order["order_id"] for o in open_orders)


def test_cancel_order(client, symbol, sym_info, clean_orders):
    mark = market.get_futures_mark_price(client, symbol)
    price = (mark * Decimal("0.85")).quantize(sym_info["tick_size"])
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])

    order = orders.place_limit_order(client, symbol, "BUY", qty, price)
    orders.cancel_order(client, symbol, order["order_id"])

    open_orders = orders.get_open_orders(client, symbol)
    assert not any(o["order_id"] == order["order_id"] for o in open_orders)


def test_cancel_all_orders(client, symbol, sym_info, clean_orders):
    mark = market.get_futures_mark_price(client, symbol)
    price = (mark * Decimal("0.85")).quantize(sym_info["tick_size"])
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])

    orders.place_limit_order(client, symbol, "BUY", qty, price)
    orders.place_limit_order(client, symbol, "BUY", qty, price)
    orders.cancel_all_orders(client, symbol)

    assert orders.get_open_orders(client, symbol) == []


def test_place_trailing_stop_order(client, symbol, sym_info, open_position):
    mark = market.get_futures_mark_price(client, symbol)
    qty = abs(open_position["amount"]).quantize(sym_info["step_size"])
    # Activation price 1% above entry — trailing starts only after reaching profit
    activation_price = (mark * Decimal("1.01")).quantize(sym_info["tick_size"])

    order = orders.place_trailing_stop_order(
        client, symbol, "SELL", qty, Decimal("1.0"), activation_price
    )

    assert order["is_algo"] is True
    assert order["order_id"] is not None

    open_orders = orders.get_open_orders(client, symbol)
    assert any(o["order_id"] == order["order_id"] for o in open_orders)

    algo_orders.cancel_algo_order(client, symbol, order["order_id"])
    open_orders_after = orders.get_open_orders(client, symbol)
    assert not any(o["order_id"] == order["order_id"] for o in open_orders_after)
