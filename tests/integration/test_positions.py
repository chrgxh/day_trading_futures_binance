from decimal import Decimal

import pytest

from utils import account, orders, positions

pytestmark = pytest.mark.integration


def test_close_position_when_none_open(client, symbol):
    positions.close_position(client, symbol)  # clear any stray state
    result = positions.close_position(client, symbol)
    assert result is None


def test_close_position(client, symbol, sym_info):
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])
    orders.place_market_order(client, symbol, "BUY", qty)
    assert len(account.get_futures_positions(client, symbol)) > 0

    result = positions.close_position(client, symbol)
    assert result is not None
    assert result["side"] == "SELL"
    assert account.get_futures_positions(client, symbol) == []
