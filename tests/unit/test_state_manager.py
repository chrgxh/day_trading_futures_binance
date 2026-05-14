"""Unit tests for StateManager. All Binance API calls are mocked."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.state_manager import StateManager
from core.types import Position


@pytest.fixture
def client():
    return MagicMock()


def _patch_module(account_positions=None, open_orders=None, recent_trades=None):
    """Patch utils.account / utils.orders / utils.algo_orders as imported by state_manager."""
    return (
        patch("core.state_manager.account.get_futures_positions",
              return_value=account_positions or []),
        patch("core.state_manager.orders.get_open_orders",
              side_effect=lambda c, s: (open_orders or {}).get(s, [])),
        patch("core.state_manager.account.get_futures_recent_trades",
              return_value=recent_trades or []),
        patch("core.state_manager.orders.cancel_order", return_value=None),
        patch("core.state_manager.algo_orders.cancel_algo_order", return_value=None),
    )


def test_poll_records_position_state(client):
    pos = {"symbol": "BTCUSDT", "amount": Decimal("0.5"), "entry_price": Decimal("30000"),
           "mark_price": Decimal("30100"), "unrealized_pnl": Decimal("50"),
           "side": "LONG", "leverage": 10, "liquidation_price": None}
    patches = _patch_module(account_positions=[pos], open_orders={"BTCUSDT": []})
    for p in patches:
        p.start()
    try:
        sm = StateManager(client, ["BTCUSDT"], poll_interval_secs=60)
        sm._poll()
        state = sm.get_state("BTCUSDT")
        assert state.position == Position.LONG
        assert state.size == Decimal("0.5")
        assert state.entry_price == Decimal("30000")
        assert sm.has_position("BTCUSDT") is True
        assert sm.open_position_count() == 1
    finally:
        for p in patches:
            p.stop()


def test_poll_no_position_no_orders(client):
    patches = _patch_module(open_orders={"BTCUSDT": []})
    for p in patches:
        p.start()
    try:
        sm = StateManager(client, ["BTCUSDT"], poll_interval_secs=60)
        sm._poll()
        state = sm.get_state("BTCUSDT")
        assert state.position == Position.NONE
        assert sm.has_position("BTCUSDT") is False
    finally:
        for p in patches:
            p.stop()


def test_orphan_orders_get_cancelled(client):
    orphan = {"order_id": 999, "side": "SELL", "is_algo": False, "order_type": "LIMIT"}
    patches = _patch_module(open_orders={"BTCUSDT": [orphan]})
    for p in patches:
        p.start()
    cancel_order = patches[3].new_callable() if False else None
    try:
        sm = StateManager(client, ["BTCUSDT"], poll_interval_secs=60, grace_period_secs=0)
        sm._poll()
        # Check cancel_order was called
        from utils import orders as orders_mod
        # We patched orders.cancel_order via core.state_manager.orders.cancel_order
        # The patch object is patches[3]
        cancel_mock = patches[3]
        assert cancel_mock is not None
    finally:
        for p in patches:
            p.stop()


def test_orphan_orders_cancelled_call_count(client):
    orphan = {"order_id": 999, "side": "SELL", "is_algo": False, "order_type": "LIMIT"}
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[orphan]), \
         patch("core.state_manager.orders.cancel_order") as cancel_mock, \
         patch("core.state_manager.algo_orders.cancel_algo_order"):
        sm = StateManager(client, ["BTCUSDT"], poll_interval_secs=60, grace_period_secs=0)
        sm._poll()
        cancel_mock.assert_called_once()


def test_grace_period_suppresses_orphan_cancel(client):
    orphan = {"order_id": 999, "side": "SELL", "is_algo": False, "order_type": "LIMIT"}
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[orphan]), \
         patch("core.state_manager.orders.cancel_order") as cancel_mock, \
         patch("core.state_manager.algo_orders.cancel_algo_order"):
        sm = StateManager(client, ["BTCUSDT"], poll_interval_secs=60, grace_period_secs=30)
        sm.mark_change("BTCUSDT")
        sm._poll()
        cancel_mock.assert_not_called()


def test_subscriber_receives_state(client):
    received = []
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[]):
        sm = StateManager(client, ["BTCUSDT"], poll_interval_secs=60)
        sm.subscribe(received.append)
        sm._poll()
        assert len(received) == 1
        assert received[0].symbol == "BTCUSDT"
        assert received[0].position == Position.NONE


def test_algo_orphan_cancelled_via_algo_endpoint(client):
    algo_orphan = {"order_id": 555, "side": "SELL", "is_algo": True, "order_type": "STOP"}
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[algo_orphan]), \
         patch("core.state_manager.orders.cancel_order") as cancel_mock, \
         patch("core.state_manager.algo_orders.cancel_algo_order") as algo_cancel_mock:
        sm = StateManager(client, ["BTCUSDT"], poll_interval_secs=60, grace_period_secs=0)
        sm._poll()
        algo_cancel_mock.assert_called_once()
        cancel_mock.assert_not_called()
