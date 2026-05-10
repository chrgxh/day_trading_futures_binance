import time
from decimal import Decimal

import pytest

from utils import account, positions as positions_mod
from utils.indicators import Position
from utils.trade_manager import TradeManager

pytestmark = pytest.mark.integration


def test_query_realized_pnl_returns_decimal_after_close(client, symbol, sym_info, open_position):
    """_query_realized_pnl returns a Decimal matching the sum of closing-side realized PnL."""
    mgr = TradeManager(client)
    mgr.register_trade(
        symbol=symbol,
        position=Position.LONG,
        size=abs(open_position["amount"]),
        entry_price=open_position["entry_price"],
        tick_size=sym_info["tick_size"],
        stop_ids=[],
        sl_limit_price=Decimal("0"),
        sl_market_price=Decimal("0"),
        ttp_id=None,
        tp_limit_id=None,
        has_order_details=False,
    )

    positions_mod.close_position(client, symbol)

    with mgr._lock:
        state = mgr._states[symbol]

    pnl = mgr._query_realized_pnl(state)

    assert isinstance(pnl, Decimal)

    # Verify the returned value matches the raw trade data independently.
    trades = account.get_futures_recent_trades(client, symbol, start_time_ms=state.registered_at_ms)
    sell_trades = [t for t in trades if t["side"] == "SELL"]
    assert len(sell_trades) > 0, "Expected at least one SELL trade after closing the position"
    assert pnl == sum(t["realized_pnl"] for t in sell_trades)
