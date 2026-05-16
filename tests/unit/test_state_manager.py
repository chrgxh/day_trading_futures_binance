"""Unit tests for StateManager. All Binance API calls are mocked.

StateManager is WebSocket-driven: `_resync()` is the full REST snapshot used at
startup and as the periodic safety net; `_handle_event()` dispatches a user-data
event by REST-refreshing the affected symbol(s) via `_refresh_symbol()`.
"""

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from core.state_manager import StateManager
from core.types import Position


@pytest.fixture
def client():
    from unittest.mock import MagicMock
    return MagicMock()


def _patch_module(account_positions=None, open_orders=None, recent_trades=None):
    """Patch utils.account / utils.orders / utils.algo_orders as imported by state_manager.

    get_futures_positions is called both with one arg (full resync) and two
    (per-symbol refresh); a return_value mock handles both.
    """
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


def test_resync_records_position_state(client):
    pos = {"symbol": "BTCUSDT", "amount": Decimal("0.5"), "entry_price": Decimal("30000"),
           "mark_price": Decimal("30100"), "unrealized_pnl": Decimal("50"),
           "side": "LONG", "leverage": 10, "liquidation_price": None}
    patches = _patch_module(account_positions=[pos], open_orders={"BTCUSDT": []})
    for p in patches:
        p.start()
    try:
        sm = StateManager(client, ["BTCUSDT"])
        sm._resync()
        state = sm.get_state("BTCUSDT")
        assert state.position == Position.LONG
        assert state.size == Decimal("0.5")
        assert state.entry_price == Decimal("30000")
        assert sm.has_position("BTCUSDT") is True
        assert sm.open_position_count() == 1
    finally:
        for p in patches:
            p.stop()


def test_resync_no_position_no_orders(client):
    patches = _patch_module(open_orders={"BTCUSDT": []})
    for p in patches:
        p.start()
    try:
        sm = StateManager(client, ["BTCUSDT"])
        sm._resync()
        state = sm.get_state("BTCUSDT")
        assert state.position == Position.NONE
        assert sm.has_position("BTCUSDT") is False
    finally:
        for p in patches:
            p.stop()


def test_orphan_orders_cancelled_call_count(client):
    orphan = {"order_id": 999, "side": "SELL", "is_algo": False, "order_type": "LIMIT"}
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[orphan]), \
         patch("core.state_manager.orders.cancel_order") as cancel_mock, \
         patch("core.state_manager.algo_orders.cancel_algo_order"):
        sm = StateManager(client, ["BTCUSDT"], grace_period_secs=0)
        sm._resync()
        cancel_mock.assert_called_once()


def test_grace_period_suppresses_orphan_cancel(client):
    orphan = {"order_id": 999, "side": "SELL", "is_algo": False, "order_type": "LIMIT"}
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[orphan]), \
         patch("core.state_manager.orders.cancel_order") as cancel_mock, \
         patch("core.state_manager.algo_orders.cancel_algo_order"):
        sm = StateManager(client, ["BTCUSDT"], grace_period_secs=30)
        sm.mark_change("BTCUSDT")
        sm._resync()
        cancel_mock.assert_not_called()


def test_subscriber_receives_state(client):
    received = []
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[]):
        sm = StateManager(client, ["BTCUSDT"])
        sm.subscribe(received.append)
        sm._resync()
        assert len(received) == 1
        assert received[0].symbol == "BTCUSDT"
        assert received[0].position == Position.NONE


def test_algo_orphan_cancelled_via_algo_endpoint(client):
    algo_orphan = {"order_id": 555, "side": "SELL", "is_algo": True, "order_type": "STOP"}
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[algo_orphan]), \
         patch("core.state_manager.orders.cancel_order") as cancel_mock, \
         patch("core.state_manager.algo_orders.cancel_algo_order") as algo_cancel_mock:
        sm = StateManager(client, ["BTCUSDT"], grace_period_secs=0)
        sm._resync()
        algo_cancel_mock.assert_called_once()
        cancel_mock.assert_not_called()


# ---------------------------------------------------------------------------
# WebSocket event handling
# ---------------------------------------------------------------------------

def test_order_trade_update_refreshes_symbol(client):
    pos = {"symbol": "BTCUSDT", "amount": Decimal("0.2"), "entry_price": Decimal("30000"),
           "mark_price": Decimal("30050"), "unrealized_pnl": Decimal("10"),
           "side": "LONG", "leverage": 10, "liquidation_price": None}
    with patch("core.state_manager.account.get_futures_positions", return_value=[pos]) as pos_mock, \
         patch("core.state_manager.orders.get_open_orders", return_value=[]), \
         patch("core.state_manager.account.get_futures_recent_trades", return_value=[]):
        sm = StateManager(client, ["BTCUSDT"])
        sm._handle_event({"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "x": "NEW"}})
        # Per-symbol refresh queries positions filtered to the symbol.
        pos_mock.assert_called_once_with(client, "BTCUSDT")
        assert sm.get_state("BTCUSDT").position == Position.LONG


def test_order_trade_update_fill_refreshes_pnl(client):
    trade = {"realized_pnl": Decimal("25"), "commission": Decimal("5")}
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[]), \
         patch("core.state_manager.account.get_futures_recent_trades", return_value=[trade]) as trades_mock:
        sm = StateManager(client, ["BTCUSDT"])
        sm._handle_event({"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "x": "TRADE"}})
        trades_mock.assert_called_once()
        assert sm.daily_pnl() == Decimal("20")  # 25 realized - 5 commission
        assert sm.trade_count() == 1


def test_account_update_refreshes_listed_symbols(client):
    with patch("core.state_manager.account.get_futures_positions", return_value=[]) as pos_mock, \
         patch("core.state_manager.orders.get_open_orders", return_value=[]):
        sm = StateManager(client, ["BTCUSDT", "ETHUSDT"])
        sm._handle_event({"e": "ACCOUNT_UPDATE", "a": {"P": [{"s": "ETHUSDT"}]}})
        pos_mock.assert_called_once_with(client, "ETHUSDT")


def test_resync_sentinel_triggers_full_resync(client):
    with patch("core.state_manager.account.get_futures_positions", return_value=[]) as pos_mock, \
         patch("core.state_manager.orders.get_open_orders", return_value=[]), \
         patch("core.state_manager.account.get_futures_recent_trades", return_value=[]):
        sm = StateManager(client, ["BTCUSDT"])
        sm._handle_event({"e": "_RESYNC"})
        # Full resync queries all positions in one unfiltered call.
        pos_mock.assert_called_once_with(client)


def test_unknown_event_type_ignored(client):
    with patch("core.state_manager.account.get_futures_positions") as pos_mock, \
         patch("core.state_manager.orders.get_open_orders"):
        sm = StateManager(client, ["BTCUSDT"])
        sm._handle_event({"e": "ACCOUNT_CONFIG_UPDATE", "ac": {"s": "BTCUSDT", "l": 10}})
        pos_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Persistent ownership store
# ---------------------------------------------------------------------------

def test_register_and_get_owner(client, tmp_path: Path):
    pf = tmp_path / "positions.json"
    sm = StateManager(client, ["BTCUSDT"], positions_file=pf)
    sm.attach_strategy("strat_a")
    sm.register_owner(
        "BTCUSDT",
        strategy_name="strat_a", side="LONG",
        entry_price=Decimal("30000"), qty=Decimal("0.1"),
        strategy_state={"entry_atr": 100.0, "r_distance": 150.0},
        orders={"stop_loss_id": 1, "tp1_id": 2},
    )
    entry = sm.get_owner("BTCUSDT")
    assert entry is not None
    assert entry["strategy"] == "strat_a"
    assert entry["entry_price"] == "30000"
    assert entry["strategy_state"]["entry_atr"] == 100.0


def test_no_store_when_positions_file_omitted(client):
    sm = StateManager(client, ["BTCUSDT"])
    assert sm.get_owner("BTCUSDT") is None
    sm.register_owner("BTCUSDT", strategy_name="x", side="LONG",
                      entry_price="1", qty="1", strategy_state={}, orders={})
    assert sm.get_owner("BTCUSDT") is None  # no-op without a file


def test_update_owner_patches_fields(client, tmp_path: Path):
    pf = tmp_path / "positions.json"
    sm = StateManager(client, ["BTCUSDT"], positions_file=pf)
    sm.attach_strategy("s")
    sm.register_owner("BTCUSDT", strategy_name="s", side="LONG",
                      entry_price="1", qty="1",
                      strategy_state={"v": 1}, orders={"stop_loss_id": 10})
    sm.update_owner("BTCUSDT", strategy_state={"v": 2}, orders={"stop_loss_id": 99})
    entry = sm.get_owner("BTCUSDT")
    assert entry["strategy_state"] == {"v": 2}
    assert entry["orders"]["stop_loss_id"] == 99


def test_load_from_existing_file_on_init(client, tmp_path: Path):
    pf = tmp_path / "positions.json"
    pf.write_text(json.dumps({
        "version": 1,
        "updated_at": "2026-05-14T00:00:00+00:00",
        "positions": {
            "BTCUSDT": {
                "strategy": "strat_a", "opened_at": "...",
                "side": "LONG", "entry_price": "30000", "qty": "0.1",
                "strategy_state": {"entry_atr": 100.0, "r_distance": 150.0},
                "orders": {"stop_loss_id": 7, "tp1_id": 8},
            }
        }
    }))
    sm = StateManager(client, ["BTCUSDT"], positions_file=pf)
    entry = sm.get_owner("BTCUSDT")
    assert entry is not None
    assert entry["strategy"] == "strat_a"
    assert entry["orders"]["stop_loss_id"] == 7


def test_resync_prunes_entries_for_closed_positions(client, tmp_path: Path):
    pf = tmp_path / "positions.json"
    pf.write_text(json.dumps({
        "version": 1, "updated_at": "...",
        "positions": {
            "BTCUSDT": {
                "strategy": "strat_a", "opened_at": "...",
                "side": "LONG", "entry_price": "30000", "qty": "0.1",
                "strategy_state": {}, "orders": {},
            }
        }
    }))
    with patch("core.state_manager.account.get_futures_positions", return_value=[]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[]):
        sm = StateManager(client, ["BTCUSDT"], grace_period_secs=0, positions_file=pf)
        sm.attach_strategy("strat_a")
        assert sm.get_owner("BTCUSDT") is not None
        sm._resync()
        # Position not on Binance → entry dropped.
        assert sm.get_owner("BTCUSDT") is None


def test_resync_prunes_entries_for_unknown_strategy(client, tmp_path: Path):
    pf = tmp_path / "positions.json"
    pf.write_text(json.dumps({
        "version": 1, "updated_at": "...",
        "positions": {
            "BTCUSDT": {
                "strategy": "removed_strategy", "opened_at": "...",
                "side": "LONG", "entry_price": "30000", "qty": "0.1",
                "strategy_state": {}, "orders": {},
            }
        }
    }))
    pos = {"symbol": "BTCUSDT", "amount": Decimal("0.1"), "entry_price": Decimal("30000"),
           "mark_price": Decimal("30100"), "unrealized_pnl": Decimal("10"),
           "side": "LONG", "leverage": 5, "liquidation_price": None}
    with patch("core.state_manager.account.get_futures_positions", return_value=[pos]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[]):
        # No strategy attached — entry references "removed_strategy".
        sm = StateManager(client, ["BTCUSDT"], grace_period_secs=0, positions_file=pf)
        sm._resync()
        assert sm.get_owner("BTCUSDT") is None


def test_resync_keeps_entry_for_live_position_with_known_strategy(client, tmp_path: Path):
    pf = tmp_path / "positions.json"
    pf.write_text(json.dumps({
        "version": 1, "updated_at": "...",
        "positions": {
            "BTCUSDT": {
                "strategy": "strat_a", "opened_at": "...",
                "side": "LONG", "entry_price": "30000", "qty": "0.1",
                "strategy_state": {"x": 1}, "orders": {"stop_loss_id": 7},
            }
        }
    }))
    pos = {"symbol": "BTCUSDT", "amount": Decimal("0.1"), "entry_price": Decimal("30000"),
           "mark_price": Decimal("30100"), "unrealized_pnl": Decimal("10"),
           "side": "LONG", "leverage": 5, "liquidation_price": None}
    with patch("core.state_manager.account.get_futures_positions", return_value=[pos]), \
         patch("core.state_manager.orders.get_open_orders", return_value=[]):
        sm = StateManager(client, ["BTCUSDT"], grace_period_secs=0, positions_file=pf)
        sm.attach_strategy("strat_a")
        sm._resync()
        entry = sm.get_owner("BTCUSDT")
        assert entry is not None
        assert entry["strategy_state"] == {"x": 1}
