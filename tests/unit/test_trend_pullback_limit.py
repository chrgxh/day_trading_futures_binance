"""Unit tests for TrendPullbackLimit.

Focus on the deterministic decision surfaces:
- regime gate (long / short EMA stack)
- entry computation + the taker-fill guard
- resting-order placement and lifecycle (regime flip / expiry)
- on_fill / on_cancel callbacks
- exit-price math + TP split
- break-even move
- serialize / adopt for restart recovery (pending + open)

Broker I/O (order placement, layered-stop placement) is mocked — it is
covered by the integration suite against the testnet.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock, patch

from core.strategies.base import LayeredStopIds
from core.strategies.trend_pullback_limit import (
    TrendPullbackLimit,
    _ManagedPosition,
    _PendingEntry,
)
from core.types import Action, Position, SymbolState

D = Decimal
MS_1H = 3_600_000
MS_4H = 14_400_000


def _c(t: int, o: float, h: float, l: float, c: float, v: float = 100.0) -> dict:
    return {"open_time": t, "open": D(str(o)), "high": D(str(h)),
            "low": D(str(l)), "close": D(str(c)), "volume": D(str(v))}


def _series(n: int, interval_ms: int, start: float, step: float) -> list[dict]:
    """A monotonic OHLCV series. step > 0 rises, step < 0 falls."""
    out: list[dict] = []
    price = start
    for i in range(n):
        o = price
        price += step
        c = price
        hi = max(o, c) + 1.0
        lo = min(o, c) - 1.0
        out.append(_c(i * interval_ms, o, hi, lo, c))
    return out


def _state(symbol="BTCUSDT", position=Position.NONE, size=D("0"),
           entry_price=D("100"), orders=None) -> SymbolState:
    return SymbolState(symbol=symbol, position=position, size=size,
                       entry_price=entry_price, mark_price=D("100"),
                       unrealized_pnl=D("0"), orders=orders or [])


def _build(params: Optional[dict] = None, has_position=False) -> TrendPullbackLimit:
    defaults = {
        "entry_interval": "1h",
        "regime_interval": "4h",
        "leverage": 5,
        "notional_per_trade_usdt": 100,
        "regime_ema_fast": 5,
        "regime_ema_slow": 10,
        "ema_fast": 5,
        "atr_period": 5,
        "entry_offset_atr_mult": 0.0,
        "entry_expiry_candles": 3,
        "stop_atr_mult": 1.5,
        "tp1_r_multiple": 1.0,
        "tp1_size_pct": 0.5,
        "tp2_r_multiple": 2.5,
        "tp2_size_pct": 0.5,
        "break_even_offset_atr_mult": 0.0,
        "stop_limit_buffer_pct": 0.0,
        "stop_market_backstop_pct": 0.1,
    }
    if params:
        defaults.update(params)

    sm = MagicMock()
    sm.has_position.return_value = has_position
    sm.get_state.return_value = _state()
    rg = MagicMock()
    rg.allow_open.return_value = True

    return TrendPullbackLimit(
        name="trend_pullback_limit",
        symbols=["BTCUSDT"],
        params=defaults,
        client=MagicMock(),
        sym_infos={"BTCUSDT": {"tick_size": D("0.1"), "step_size": D("0.001")}},
        state_manager=sm,
        risk_guard=rg,
        live_trade_manager=None,
    )


def _seed_uptrend(s: TrendPullbackLimit) -> None:
    s._buffers["BTCUSDT"]["4h"] = _series(60, MS_4H, 100.0, 1.0)
    s._buffers["BTCUSDT"]["1h"] = _series(60, MS_1H, 100.0, 1.0)


def _seed_downtrend(s: TrendPullbackLimit) -> None:
    s._buffers["BTCUSDT"]["4h"] = _series(60, MS_4H, 200.0, -1.0)
    s._buffers["BTCUSDT"]["1h"] = _series(60, MS_1H, 200.0, -1.0)


# ---------------------------------------------------------------------------
# Constructor + intervals
# ---------------------------------------------------------------------------

def test_intervals_derived_from_params():
    s = _build({"entry_interval": "15m", "regime_interval": "1h"})
    assert s.intervals == ["15m", "1h"]


def test_intervals_deduplicated():
    s = _build({"entry_interval": "1h", "regime_interval": "1h"})
    assert s.intervals == ["1h"]


def test_attaches_strategy_to_state_manager():
    s = _build()
    s.state_manager.attach_strategy.assert_called_with("trend_pullback_limit")


# ---------------------------------------------------------------------------
# Regime gate
# ---------------------------------------------------------------------------

def test_regime_long_ok_on_uptrend():
    s = _build()
    _seed_uptrend(s)
    long_ok, short_ok, _ = s._regime_summary("BTCUSDT")
    assert long_ok and not short_ok


def test_regime_short_ok_on_downtrend():
    s = _build()
    _seed_downtrend(s)
    long_ok, short_ok, _ = s._regime_summary("BTCUSDT")
    assert short_ok and not long_ok


def test_regime_warmup_blocks_both_sides():
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = _series(5, MS_4H, 100.0, 1.0)
    long_ok, short_ok, diag = s._regime_summary("BTCUSDT")
    assert not long_ok and not short_ok
    assert "warmup" in diag


# ---------------------------------------------------------------------------
# Entry computation
# ---------------------------------------------------------------------------

def test_compute_entry_long_signal_on_uptrend():
    s = _build()
    _seed_uptrend(s)
    result = s._compute_entry("BTCUSDT")
    assert result is not None
    signal, entry_atr, level = result
    assert signal.action == Action.OPEN_LONG
    assert entry_atr > 0
    # Resting level is the entry-interval EMA — below the latest close.
    assert level < float(s._buffers["BTCUSDT"]["1h"][-1]["close"])


def test_compute_entry_blocks_when_regime_flat():
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = _series(60, MS_4H, 100.0, 0.0)  # flat → no stack
    s._buffers["BTCUSDT"]["1h"] = _series(60, MS_1H, 100.0, 0.0)
    assert s._compute_entry("BTCUSDT") is None


def test_compute_entry_taker_guard_blocks_close_below_level():
    """If close has dropped below the EMA level, the BUY limit would fill as a
    taker — the entry is rejected."""
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = _series(60, MS_4H, 100.0, 1.0)
    # Uptrend then a sharp final dip: close ends well below the lagging EMA.
    entry = _series(58, MS_1H, 100.0, 1.0)
    last_t = entry[-1]["open_time"]
    entry.append(_c(last_t + MS_1H, 158.0, 159.0, 120.0, 122.0))
    entry.append(_c(last_t + 2 * MS_1H, 122.0, 123.0, 118.0, 119.0))
    s._buffers["BTCUSDT"]["1h"] = entry
    assert s._compute_entry("BTCUSDT") is None


# ---------------------------------------------------------------------------
# Exit-price math
# ---------------------------------------------------------------------------

def test_compute_exit_prices_long():
    s = _build()
    stop, tp1, tp2, r = s._compute_exit_prices(D("100"), 2.0, is_long=True, tick_size=D("0.1"))
    assert r == 3.0                       # stop_atr_mult 1.5 * atr 2.0
    assert stop == D("97.0")              # 100 - 3
    assert tp1 == D("103.0")              # 100 + 1.0R
    assert tp2 == D("107.5")              # 100 + 2.5R


def test_compute_exit_prices_short():
    s = _build()
    stop, tp1, tp2, r = s._compute_exit_prices(D("100"), 2.0, is_long=False, tick_size=D("0.1"))
    assert stop == D("103.0")
    assert tp1 == D("97.0")
    assert tp2 == D("92.5")


def test_split_tp_qty_even():
    s = _build()
    tp1, tp2 = s._split_tp_qty(D("10"), D("0.001"))
    assert tp1 == D("5")
    assert tp2 == D("5")


def test_split_tp_qty_single_tp_when_tp2_zero():
    s = _build({"tp2_size_pct": 0.0})
    tp1, tp2 = s._split_tp_qty(D("10"), D("0.001"))
    assert tp1 == D("5")
    assert tp2 == D("0")


# ---------------------------------------------------------------------------
# Resting-order placement + lifecycle
# ---------------------------------------------------------------------------

@patch("core.strategies.base.orders")
def test_place_resting_entry_registers_pending(mock_orders):
    s = _build()
    _seed_uptrend(s)
    mock_orders.place_limit_order.return_value = {"order_id": 111}
    result = s._compute_entry("BTCUSDT")
    assert result is not None
    signal, entry_atr, level = result
    s._place_resting_entry(signal, entry_atr, level)

    assert "BTCUSDT" in s._pending
    assert s._pending["BTCUSDT"].order_id == 111
    s.state_manager.register_pending_entry.assert_called_once()
    # GTC resting order, not IOC/GTX.
    assert mock_orders.place_limit_order.call_args.kwargs["time_in_force"] == "GTC"


@patch("core.strategies.base.orders")
def test_manage_pending_cancels_on_regime_flip(mock_orders):
    s = _build()
    _seed_downtrend(s)  # regime now favours SHORT
    s._pending["BTCUSDT"] = _PendingEntry(
        symbol="BTCUSDT", side="LONG", order_id=111, price=D("100"),
        qty=D("1"), entry_atr=2.0, placed_candle_open_time=0,
    )
    s._manage_pending("BTCUSDT")
    assert "BTCUSDT" not in s._pending
    mock_orders.cancel_order.assert_called_once()
    s.state_manager.clear_pending_entry.assert_called_with("BTCUSDT")


@patch("core.strategies.base.orders")
def test_manage_pending_cancels_on_expiry(mock_orders):
    s = _build({"entry_expiry_candles": 3})
    _seed_uptrend(s)  # regime still supports LONG
    s._pending["BTCUSDT"] = _PendingEntry(
        symbol="BTCUSDT", side="LONG", order_id=111, price=D("100"),
        qty=D("1"), entry_atr=2.0, placed_candle_open_time=0,
    )
    # The 1h buffer has 60 candles past open_time 0 — well over the expiry.
    s._manage_pending("BTCUSDT")
    assert "BTCUSDT" not in s._pending
    mock_orders.cancel_order.assert_called_once()


@patch("core.strategies.base.orders")
def test_manage_pending_holds_within_expiry(mock_orders):
    s = _build({"entry_expiry_candles": 100})
    _seed_uptrend(s)
    s._pending["BTCUSDT"] = _PendingEntry(
        symbol="BTCUSDT", side="LONG", order_id=111, price=D("100"),
        qty=D("1"), entry_atr=2.0, placed_candle_open_time=0,
    )
    s._manage_pending("BTCUSDT")
    assert "BTCUSDT" in s._pending
    mock_orders.cancel_order.assert_not_called()


def test_on_entry_cancel_clears_pending():
    s = _build()
    s._pending["BTCUSDT"] = _PendingEntry(
        symbol="BTCUSDT", side="LONG", order_id=111, price=D("100"),
        qty=D("1"), entry_atr=2.0, placed_candle_open_time=0,
    )
    s._on_entry_cancel("BTCUSDT")
    assert "BTCUSDT" not in s._pending


# ---------------------------------------------------------------------------
# on_fill callback
# ---------------------------------------------------------------------------

@patch("core.strategies.trend_pullback_limit.orders")
def test_on_entry_fill_places_exits_and_records_managed(mock_orders):
    s = _build()
    _seed_uptrend(s)
    s._pending["BTCUSDT"] = _PendingEntry(
        symbol="BTCUSDT", side="LONG", order_id=111, price=D("100"),
        qty=D("1"), entry_atr=2.0, placed_candle_open_time=0,
    )
    mock_orders.place_tp_limit_order.side_effect = [
        {"order_id": 201}, {"order_id": 202},
    ]
    s._place_layered_stop = MagicMock(return_value=LayeredStopIds(limit_id=301, market_id=302))

    state = _state(position=Position.LONG, size=D("1"), entry_price=D("100"))
    s._on_entry_fill(state)

    assert "BTCUSDT" not in s._pending
    mp = s._managed["BTCUSDT"]
    assert mp.side == "LONG"
    assert mp.entry_price == D("100")
    assert mp.stop_ids.limit_id == 301
    assert mp.tp1_order_id == 201
    assert mp.tp2_order_id == 202
    s.state_manager.register_owner.assert_called_once()


def test_on_entry_fill_emergency_closes_when_stop_fails():
    s = _build()
    _seed_uptrend(s)
    s._pending["BTCUSDT"] = _PendingEntry(
        symbol="BTCUSDT", side="LONG", order_id=111, price=D("100"),
        qty=D("1"), entry_atr=2.0, placed_candle_open_time=0,
    )
    s._place_layered_stop = MagicMock(return_value=None)
    s._emergency_close = MagicMock()

    with patch("core.strategies.trend_pullback_limit.orders"):
        s._on_entry_fill(_state(position=Position.LONG, size=D("1"), entry_price=D("100")))

    s._emergency_close.assert_called_once_with("BTCUSDT")
    assert "BTCUSDT" not in s._managed


# ---------------------------------------------------------------------------
# Break-even move
# ---------------------------------------------------------------------------

def test_manage_position_moves_stop_to_break_even_after_partial_fill():
    s = _build()
    s._managed["BTCUSDT"] = _ManagedPosition(
        symbol="BTCUSDT", side="LONG", entry_price=D("100"), entry_atr=2.0,
        r_distance=3.0, initial_qty=D("10"), entry_candle_open_time=0,
        stop_ids=LayeredStopIds(limit_id=301, market_id=302),
    )
    # Live size shrank vs initial → a TP partially filled.
    s.state_manager.get_state.return_value = _state(
        position=Position.LONG, size=D("5"))
    s._replace_layered_stop = MagicMock(
        return_value=LayeredStopIds(limit_id=401, market_id=402))

    s._manage_position("BTCUSDT")

    mp = s._managed["BTCUSDT"]
    assert mp.stop_moved_to_be is True
    assert mp.stop_ids.limit_id == 401


def test_manage_position_no_break_even_when_size_unchanged():
    s = _build()
    s._managed["BTCUSDT"] = _ManagedPosition(
        symbol="BTCUSDT", side="LONG", entry_price=D("100"), entry_atr=2.0,
        r_distance=3.0, initial_qty=D("10"), entry_candle_open_time=0,
        stop_ids=LayeredStopIds(limit_id=301, market_id=302),
    )
    s.state_manager.get_state.return_value = _state(
        position=Position.LONG, size=D("10"))
    s._replace_layered_stop = MagicMock()

    s._manage_position("BTCUSDT")
    s._replace_layered_stop.assert_not_called()


# ---------------------------------------------------------------------------
# Persistence: serialize + adopt
# ---------------------------------------------------------------------------

def test_serialize_state_round_trips_managed_fields():
    s = _build()
    s._managed["BTCUSDT"] = _ManagedPosition(
        symbol="BTCUSDT", side="LONG", entry_price=D("100"), entry_atr=2.0,
        r_distance=3.0, initial_qty=D("10"), entry_candle_open_time=42,
        stop_moved_to_be=True,
    )
    blob = s.serialize_state("BTCUSDT")
    assert blob["entry_atr"] == 2.0
    assert blob["r_distance"] == 3.0
    assert blob["entry_candle_open_time"] == 42
    assert blob["stop_moved_to_be"] is True


def test_adopt_open_reconciles_live_orders():
    s = _build()
    s.state_manager.get_state.return_value = _state(
        position=Position.LONG, size=D("10"), entry_price=D("100"),
        orders=[{"order_id": 301}, {"order_id": 302}, {"order_id": 201}],
    )
    entry = {
        "status": "open", "strategy": "trend_pullback_limit", "side": "LONG",
        "entry_price": "100", "qty": "10",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 3.0,
                           "entry_candle_open_time": 42, "stop_moved_to_be": False},
        "orders": {"stop_limit_id": 301, "stop_market_id": 302,
                   "tp1_id": 201, "tp2_id": 202},
    }
    s.adopt("BTCUSDT", entry)
    mp = s._managed["BTCUSDT"]
    assert mp.stop_ids.limit_id == 301 and mp.stop_ids.market_id == 302
    assert mp.tp1_order_id == 201
    assert mp.tp2_order_id is None       # 202 not in live orders → dropped


def test_adopt_pending_rearms_resting_order():
    s = _build()
    s.state_manager.get_state.return_value = _state(
        position=Position.NONE, orders=[{"order_id": 111}])
    entry = {
        "status": "pending", "strategy": "trend_pullback_limit", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "placed_candle_open_time": 7},
        "orders": {"entry_id": 111},
    }
    s.adopt("BTCUSDT", entry)
    assert "BTCUSDT" in s._pending
    assert s._pending["BTCUSDT"].order_id == 111
    s.state_manager.register_pending_entry.assert_called_once()


def test_adopt_pending_opens_position_when_filled_during_downtime():
    s = _build()
    _seed_uptrend(s)
    s.state_manager.get_state.return_value = _state(
        position=Position.LONG, size=D("1"), entry_price=D("100"))
    s._place_layered_stop = MagicMock(
        return_value=LayeredStopIds(limit_id=301, market_id=302))
    entry = {
        "status": "pending", "strategy": "trend_pullback_limit", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "placed_candle_open_time": 7},
        "orders": {"entry_id": 111},
    }
    with patch("core.strategies.trend_pullback_limit.orders"):
        s.adopt("BTCUSDT", entry)

    assert "BTCUSDT" in s._managed
    assert "BTCUSDT" not in s._pending
    s.state_manager.clear_pending_entry.assert_called_with("BTCUSDT")


def test_adopt_pending_clears_when_order_gone():
    s = _build()
    s.state_manager.get_state.return_value = _state(
        position=Position.NONE, orders=[])  # order 111 absent
    entry = {
        "status": "pending", "strategy": "trend_pullback_limit", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "placed_candle_open_time": 7},
        "orders": {"entry_id": 111},
    }
    s.adopt("BTCUSDT", entry)
    assert "BTCUSDT" not in s._pending
    assert "BTCUSDT" not in s._managed
    s.state_manager.clear_pending_entry.assert_called_with("BTCUSDT")


# ---------------------------------------------------------------------------
# Tick routing
# ---------------------------------------------------------------------------

def test_regime_candle_only_buffers_no_action():
    s = _build()
    s._buffers["BTCUSDT"]["1h"] = []
    s._buffers["BTCUSDT"]["4h"] = _series(40, MS_4H, 100.0, 1.0)
    s.on_candle("BTCUSDT", "4h", _c(40 * MS_4H, 140, 142, 139, 141))
    assert "BTCUSDT" not in s._managed
    assert "BTCUSDT" not in s._pending


@patch("core.strategies.base.orders")
def test_tick_places_resting_entry_on_valid_uptrend(mock_orders):
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = _series(60, MS_4H, 100.0, 1.0)
    s._buffers["BTCUSDT"]["1h"] = _series(59, MS_1H, 100.0, 1.0)
    mock_orders.place_limit_order.return_value = {"order_id": 111}

    s.on_candle("BTCUSDT", "1h", _c(59 * MS_1H, 159, 161, 158, 160))
    assert "BTCUSDT" in s._pending
