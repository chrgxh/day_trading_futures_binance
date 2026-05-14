"""Unit tests for the Strategy ABC behaviour shared across all strategies."""

from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock

from core.strategies.base import Strategy
from core.types import Action, Signal


class _StubStrategy(Strategy):
    """Minimal Strategy used to test base-class behaviour. Tracks every hook call."""

    def __init__(self, signal: Optional[Signal] = None, **kwargs):
        super().__init__(**kwargs)
        self._signal = signal
        self.compute_calls = 0
        self.execute_calls: list[Signal] = []

    def compute_signal(self, symbol, candles):
        self.compute_calls += 1
        return self._signal

    def execute_open(self, signal):
        self.execute_calls.append(signal)


def build(signal: Optional[Signal] = None, has_position=False, allow_open=True,
          intervals=("5m",)):
    sm = MagicMock()
    sm.has_position.return_value = has_position
    rg = MagicMock()
    rg.allow_open.return_value = allow_open
    return _StubStrategy(
        signal=signal,
        name="stub",
        intervals=list(intervals),
        symbols=["BTCUSDT"],
        params={},
        client=MagicMock(),
        sym_infos={"BTCUSDT": {}},
        state_manager=sm,
        risk_guard=rg,
        live_trade_manager=None,
    )


def test_buffer_appends_new_closed_candle():
    s = build()
    s.warmup("BTCUSDT", "5m", [{"open_time": 1000}, {"open_time": 2000}])
    s.on_candle("BTCUSDT", "5m", {"open_time": 3000})
    assert [c["open_time"] for c in s._buffers["BTCUSDT"]["5m"]] == [1000, 2000, 3000]


def test_buffer_replaces_open_candle_for_same_period():
    s = build()
    s.warmup("BTCUSDT", "5m", [{"open_time": 1000, "close": 1}])
    s.on_candle("BTCUSDT", "5m", {"open_time": 1000, "close": 2})
    assert s._buffers["BTCUSDT"]["5m"][-1]["close"] == 2
    assert len(s._buffers["BTCUSDT"]["5m"]) == 1


def test_buffers_are_isolated_per_interval():
    s = build(intervals=("5m", "1h"))
    s.warmup("BTCUSDT", "5m", [{"open_time": 1000}])
    s.warmup("BTCUSDT", "1h", [{"open_time": 2000}])
    s.on_candle("BTCUSDT", "5m", {"open_time": 3000})
    assert [c["open_time"] for c in s._buffers["BTCUSDT"]["5m"]] == [1000, 3000]
    assert [c["open_time"] for c in s._buffers["BTCUSDT"]["1h"]] == [2000]


def test_tick_skips_compute_when_symbol_already_held():
    s = build(signal=Signal(action=Action.OPEN_LONG, symbol="BTCUSDT"), has_position=True)
    s.warmup("BTCUSDT", "5m", [{"open_time": 1000}])
    s.on_candle("BTCUSDT", "5m", {"open_time": 2000})
    assert s.compute_calls == 0
    assert s.execute_calls == []


def test_tick_calls_compute_when_symbol_free():
    s = build(signal=Signal(action=Action.HOLD, symbol="BTCUSDT"))
    s.warmup("BTCUSDT", "5m", [{"open_time": 1000}])
    s.on_candle("BTCUSDT", "5m", {"open_time": 2000})
    assert s.compute_calls == 1


def test_open_signal_passes_through_risk_guard():
    sig = Signal(action=Action.OPEN_LONG, symbol="BTCUSDT",
                 entry_price=Decimal("100"), stop_loss_price=Decimal("99"),
                 take_profit_price=Decimal("103"))
    s = build(signal=sig, allow_open=True)
    s.warmup("BTCUSDT", "5m", [{"open_time": 1000}])
    s.on_candle("BTCUSDT", "5m", {"open_time": 2000})
    assert s.execute_calls == [sig]
    s.risk_guard.allow_open.assert_called_once_with("BTCUSDT", s)


def test_open_signal_blocked_by_risk_guard():
    sig = Signal(action=Action.OPEN_LONG, symbol="BTCUSDT",
                 entry_price=Decimal("100"), stop_loss_price=Decimal("99"),
                 take_profit_price=Decimal("103"))
    s = build(signal=sig, allow_open=False)
    s.warmup("BTCUSDT", "5m", [{"open_time": 1000}])
    s.on_candle("BTCUSDT", "5m", {"open_time": 2000})
    assert s.execute_calls == []


def test_hold_signal_does_not_call_execute():
    sig = Signal(action=Action.HOLD, symbol="BTCUSDT")
    s = build(signal=sig)
    s.warmup("BTCUSDT", "5m", [{"open_time": 1000}])
    s.on_candle("BTCUSDT", "5m", {"open_time": 2000})
    s.risk_guard.allow_open.assert_not_called()
    assert s.execute_calls == []


def test_tick_error_does_not_propagate():
    class Boom(_StubStrategy):
        def compute_signal(self, symbol, candles):
            raise RuntimeError("boom")

    sm = MagicMock(); sm.has_position.return_value = False
    rg = MagicMock(); rg.allow_open.return_value = True
    s = Boom(name="boom", intervals=["5m"], symbols=["BTCUSDT"], params={},
             client=MagicMock(), sym_infos={"BTCUSDT": {}},
             state_manager=sm, risk_guard=rg, live_trade_manager=None)
    s.warmup("BTCUSDT", "5m", [{"open_time": 1000}])
    # Should NOT raise — exceptions are caught and logged.
    s.on_candle("BTCUSDT", "5m", {"open_time": 2000})


def test_candle_limit_truncates_buffer():
    s = build()
    s.warmup("BTCUSDT", "5m", [])
    s.candle_limit = lambda interval: 3
    for t in [1000, 2000, 3000, 4000, 5000]:
        s.on_candle("BTCUSDT", "5m", {"open_time": t})
    assert [c["open_time"] for c in s._buffers["BTCUSDT"]["5m"]] == [3000, 4000, 5000]


def test_empty_intervals_rejected():
    import pytest
    sm = MagicMock(); rg = MagicMock()
    with pytest.raises(ValueError):
        _StubStrategy(
            name="x", intervals=[], symbols=["BTCUSDT"], params={},
            client=MagicMock(), sym_infos={"BTCUSDT": {}},
            state_manager=sm, risk_guard=rg, live_trade_manager=None,
        )
