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


def test_get_order(client, symbol, sym_info, clean_orders):
    mark = market.get_futures_mark_price(client, symbol)
    price = (mark * Decimal("0.85")).quantize(sym_info["tick_size"])
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])

    order = orders.place_limit_order(client, symbol, "BUY", qty, price)
    fetched = orders.get_order(client, symbol, order["order_id"])

    assert fetched["order_id"] == order["order_id"]
    assert fetched["status"] == "NEW"
    assert fetched["is_algo"] is False


def test_place_post_only_limit_order_maker(client, symbol, sym_info, clean_orders):
    """GTX order placed well below market should sit as a maker (not be rejected)."""
    mark = market.get_futures_mark_price(client, symbol)
    price = (mark * Decimal("0.85")).quantize(sym_info["tick_size"])
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])

    order = orders.place_limit_order(client, symbol, "BUY", qty, price, time_in_force="GTX")
    assert order["is_algo"] is False
    assert order["order_id"] is not None

    fetched = orders.get_order(client, symbol, order["order_id"])
    assert fetched["status"] == "NEW"


def test_gtx_order_at_best_bid_queues_as_maker(client, symbol, sym_info, clean_orders):
    """GTX BUY placed at best bid should queue in the book (not be rejected)."""
    best_bid, _ = market.get_futures_best_bid_ask(client, symbol)
    price = best_bid.quantize(sym_info["tick_size"])
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])

    order = orders.place_limit_order(client, symbol, "BUY", qty, price, time_in_force="GTX")
    assert order["order_id"] is not None

    fetched = orders.get_order(client, symbol, order["order_id"])
    assert fetched["status"] == "NEW"


# PostOnlyRejected (GTX taker rejection) cannot be reliably triggered on testnet —
# the order book is too sparse to guarantee an immediate match even when priced above
# the best ask. This is confirmed to raise APIError(-5022) on mainnet (see bot logs).


def test_place_tp_limit_order(client, symbol, sym_info, open_position):
    """TP limit at +3% above mark should sit on the book as a maker reduceOnly order."""
    mark = market.get_futures_mark_price(client, symbol)
    qty = abs(open_position["amount"]).quantize(sym_info["step_size"])
    tp_price = (mark * Decimal("1.03")).quantize(sym_info["tick_size"])

    order = orders.place_tp_limit_order(client, symbol, "SELL", qty, tp_price)

    assert order["is_algo"] is False
    assert order["order_id"] is not None
    assert order["status"] == "NEW"

    open_orders = orders.get_open_orders(client, symbol)
    assert any(o["order_id"] == order["order_id"] for o in open_orders)

    orders.cancel_order(client, symbol, order["order_id"])
    open_orders_after = orders.get_open_orders(client, symbol)
    assert not any(o["order_id"] == order["order_id"] for o in open_orders_after)


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
