"""Unit tests for the LiveTradeManager base class."""

from decimal import Decimal
from unittest.mock import MagicMock

from core.strategies.live_trade_manager import LiveTradeManager
from core.types import Position, SymbolState


def make_strategy(symbols):
    s = MagicMock()
    s.name = "test"
    s.symbols = symbols
    return s


def make_state(symbol, position, size=Decimal("1")):
    return SymbolState(symbol=symbol, position=position, size=size,
                       entry_price=Decimal("100"), mark_price=Decimal("100"),
                       unrealized_pnl=Decimal("0"), orders=[])


def test_attach_sets_strategy_reference():
    ltm = LiveTradeManager(params={})
    strat = make_strategy(["BTCUSDT"])
    ltm.attach(strat)
    assert ltm.strategy is strat


def test_register_open_marks_symbol_held_and_fires_on_open():
    fired: list = []

    class T(LiveTradeManager):
        def on_open(self, symbol):
            fired.append(symbol)

    ltm = T(params={})
    ltm.attach(make_strategy(["BTCUSDT"]))
    ltm.register_open("BTCUSDT")
    assert "BTCUSDT" in ltm._held
    assert fired == ["BTCUSDT"]


def test_on_state_update_ignores_unrelated_symbols():
    fired: list = []

    class T(LiveTradeManager):
        def on_update(self, state):
            fired.append(state.symbol)

    ltm = T(params={})
    ltm.attach(make_strategy(["BTCUSDT"]))
    ltm.register_open("BTCUSDT")
    ltm.on_state_update(make_state("ETHUSDT", Position.LONG))
    assert fired == []


def test_on_state_update_fires_on_update_while_held():
    fired: list = []

    class T(LiveTradeManager):
        def on_update(self, state):
            fired.append(state.symbol)

    ltm = T(params={})
    ltm.attach(make_strategy(["BTCUSDT"]))
    ltm.register_open("BTCUSDT")
    ltm.on_state_update(make_state("BTCUSDT", Position.LONG))
    assert fired == ["BTCUSDT"]


def test_on_state_update_fires_on_close_and_removes_from_held():
    fired: list = []

    class T(LiveTradeManager):
        def on_close(self, symbol):
            fired.append(symbol)

    ltm = T(params={})
    ltm.attach(make_strategy(["BTCUSDT"]))
    ltm.register_open("BTCUSDT")
    ltm.on_state_update(make_state("BTCUSDT", Position.NONE, size=Decimal("0")))
    assert fired == ["BTCUSDT"]
    assert "BTCUSDT" not in ltm._held


def test_on_state_update_does_nothing_when_never_held():
    fired_open: list = []
    fired_close: list = []
    fired_update: list = []

    class T(LiveTradeManager):
        def on_open(self, symbol):
            fired_open.append(symbol)
        def on_close(self, symbol):
            fired_close.append(symbol)
        def on_update(self, state):
            fired_update.append(state.symbol)

    ltm = T(params={})
    ltm.attach(make_strategy(["BTCUSDT"]))
    # Never registered → state update for a flat symbol fires nothing.
    ltm.on_state_update(make_state("BTCUSDT", Position.NONE, size=Decimal("0")))
    assert fired_open == [] and fired_close == [] and fired_update == []
