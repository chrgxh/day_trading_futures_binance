"""Unit tests for AdaptiveTrendPullback.

Focus on the deterministic decision surfaces:
- regime bias
- entry gate compositing
- trend-invalidation truth table
- dead-trade gate (including the candle-count threshold)

Execution paths (IOC chase, order placement, trail cancel/replace) are skipped
here — they are I/O-bound and covered by integration tests against the testnet.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from core.strategies.adaptive_trend_pullback import (
    AdaptiveTrendPullback,
    _EntryIndicators,
    _ManagedPosition,
)
from core.types import Action, Position, SymbolState


D = Decimal
MS_30M = 1_800_000
MS_4H = 14_400_000


def _c(t: int, o: float, h: float, l: float, c: float, v: float) -> dict:
    return {"open_time": t, "open": D(str(o)), "high": D(str(h)),
            "low": D(str(l)), "close": D(str(c)), "volume": D(str(v))}


def _state(symbol="BTCUSDT", position=Position.NONE, size=D("0")) -> SymbolState:
    return SymbolState(symbol=symbol, position=position, size=size,
                       entry_price=D("100"), mark_price=D("100"),
                       unrealized_pnl=D("0"), orders=[])


def _build(params: Optional[dict] = None, has_position=False) -> AdaptiveTrendPullback:
    """Build an AdaptiveTrendPullback wired to mocks. Override params via the arg."""
    defaults = {
        "entry_interval": "30m",
        "regime_interval": "4h",
        "leverage": 5,
        "notional_per_trade_usdt": 100,
        "regime_ema_fast": 5,
        "regime_ema_slow": 10,
        "regime_slope_lookback": 2,
        "ema_fast": 5,
        "slope_lookback": 2,
        "adx_period": 5,
        "adx_min": 20,
        "atr_period": 5,
        "atr_sma_period": 5,
        "rsi_period": 5,
        "rsi_max_long": 70,
        "rsi_min_short": 30,
        "volume_sma_period": 5,
        "pullback_lookback": 3,
        "pullback_proximity_pct": 1.0,
        "vwap_enabled": False,
        "stop_atr_mult": 1.5,
        "tp1_r_multiple": 1.5,
        "tp1_size_pct": 0.4,
        "trail_atr_mult": 2.0,
        "invalidation_structure_lookback": 10,
        "invalidation_strong_close_atr_mult": 0.5,
        "invalidation_momentum_lookback": 3,
        "invalidation_momentum_adx_drop": 5,
        "invalidation_momentum_adx_floor": 20,
        "dead_trade_min_candles": 5,
        "dead_trade_adx_lookback": 3,
        "dead_trade_adx_floor": 22,
        "dead_trade_r_floor": 1.0,
    }
    if params:
        defaults.update(params)

    sm = MagicMock()
    sm.has_position.return_value = has_position
    sm.get_state.return_value = _state()
    rg = MagicMock()
    rg.allow_open.return_value = True

    s = AdaptiveTrendPullback(
        name="adaptive_trend_pullback",
        symbols=["BTCUSDT"],
        params=defaults,
        client=MagicMock(),
        sym_infos={"BTCUSDT": {"tick_size": D("0.1"), "step_size": D("0.001")}},
        state_manager=sm,
        risk_guard=rg,
        live_trade_manager=None,
    )
    return s


# ---------------------------------------------------------------------------
# Constructor + intervals
# ---------------------------------------------------------------------------

def test_intervals_derived_from_params():
    s = _build({"entry_interval": "15m", "regime_interval": "1h"})
    assert s.intervals == ["15m", "1h"]


def test_4h_candle_only_buffers_no_action():
    s = _build()
    s._buffers["BTCUSDT"]["30m"] = []
    s._buffers["BTCUSDT"]["4h"] = [_c(i * MS_4H, 100, 101, 99, 100, 10) for i in range(50)]
    # 4h tick should not call into entry logic — execute_open shouldn't fire.
    s.on_candle("BTCUSDT", "4h", _c(50 * MS_4H, 100, 101, 99, 100, 10))
    assert "BTCUSDT" not in s._managed


# ---------------------------------------------------------------------------
# Regime bias
# ---------------------------------------------------------------------------

def test_regime_long_when_close_above_emas_and_slope_up():
    s = _build()
    # Strong uptrend
    s._buffers["BTCUSDT"]["4h"] = [
        _c(i * MS_4H, 100 + i, 101 + i, 99 + i, 100 + i, 10)
        for i in range(50)
    ]
    long_ok, short_ok = s._regime_bias("BTCUSDT")
    assert long_ok is True
    assert short_ok is False


def test_regime_short_when_close_below_emas_and_slope_down():
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = [
        _c(i * MS_4H, 200 - i, 201 - i, 199 - i, 200 - i, 10)
        for i in range(50)
    ]
    long_ok, short_ok = s._regime_bias("BTCUSDT")
    assert long_ok is False
    assert short_ok is True


def test_regime_neither_when_flat():
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = [_c(i * MS_4H, 100, 101, 99, 100, 10) for i in range(50)]
    long_ok, short_ok = s._regime_bias("BTCUSDT")
    assert long_ok is False
    assert short_ok is False


def test_regime_returns_false_when_insufficient_history():
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = [_c(i * MS_4H, 100, 101, 99, 100, 10) for i in range(5)]
    long_ok, short_ok = s._regime_bias("BTCUSDT")
    assert (long_ok, short_ok) == (False, False)


# ---------------------------------------------------------------------------
# Entry gate compositing
# ---------------------------------------------------------------------------

def test_no_signal_without_regime_bias():
    s = _build()
    # Flat 4h
    s._buffers["BTCUSDT"]["4h"] = [_c(i * MS_4H, 100, 101, 99, 100, 10) for i in range(50)]
    # Strong 30m uptrend with all gates passing — still rejected by regime
    s._buffers["BTCUSDT"]["30m"] = [
        _c(i * MS_30M, 100 + i, 101 + i, 99 + i, 100 + i, 100)
        for i in range(60)
    ]
    assert s._compute_entry("BTCUSDT") is None


def test_long_entry_fires_with_clean_setup():
    s = _build({
        "pullback_proximity_pct": 100.0,  # accept any low as a "pullback"
        "adx_min": 0,                     # don't fail the test on the synthetic series
        "rsi_max_long": 101,
    })
    # 4h uptrend
    s._buffers["BTCUSDT"]["4h"] = [
        _c(i * MS_4H, 100 + i, 101 + i, 99 + i, 100 + i, 10)
        for i in range(60)
    ]
    # 30m: 60 bars rising, last bar is bullish with volume spike
    bars = []
    for i in range(60):
        price = 100 + i * 0.5
        bars.append(_c(i * MS_30M, price, price + 0.6, price - 0.4, price + 0.5, 100))
    # Force the last bar to be a strong bullish close with high volume
    last = bars[-1]
    last["open"] = D("129.5")
    last["close"] = D("130.5")
    last["high"] = D("130.7")
    last["low"] = D("129.3")
    last["volume"] = D("10000")  # massively above SMA
    s._buffers["BTCUSDT"]["30m"] = bars

    result = s._compute_entry("BTCUSDT")
    assert result is not None
    signal, entry_atr = result
    assert signal.action == Action.OPEN_LONG
    assert signal.entry_price > 0
    assert signal.stop_loss_price < signal.entry_price
    assert signal.take_profit_price > signal.entry_price
    assert entry_atr > 0


def test_long_entry_rejected_when_volume_below_sma():
    s = _build({"pullback_proximity_pct": 100.0, "adx_min": 0, "rsi_max_long": 100})
    s._buffers["BTCUSDT"]["4h"] = [
        _c(i * MS_4H, 100 + i, 101 + i, 99 + i, 100 + i, 10) for i in range(60)
    ]
    bars = []
    for i in range(60):
        price = 100 + i * 0.5
        bars.append(_c(i * MS_30M, price, price + 0.6, price - 0.4, price + 0.5, 1000))
    # Last bar volume tiny → fails vol_ok
    last = bars[-1]
    last["open"] = D("129.5"); last["close"] = D("130.5"); last["high"] = D("130.7"); last["low"] = D("129.3")
    last["volume"] = D("1")
    s._buffers["BTCUSDT"]["30m"] = bars

    assert s._compute_entry("BTCUSDT") is None


# ---------------------------------------------------------------------------
# Pullback gate (directional)
# ---------------------------------------------------------------------------

def _ind(pullback_bars, ema_now, vwap_now=None):
    """Build an _EntryIndicators with only the fields _pullback_ok reads."""
    return _EntryIndicators(
        candles=pullback_bars,
        close=0.0, prev_close=0.0, open_=0.0, volume=0.0,
        ema_now=ema_now, atr_now=0.0, atr_sma=0.0,
        adx_now=0.0, rsi_now=0.0, vol_sma=0.0,
        vwap_now=vwap_now,
        pullback_bars=pullback_bars,
        pullback_high=max(float(c["high"]) for c in pullback_bars),
        pullback_low=min(float(c["low"]) for c in pullback_bars),
    )


def test_pullback_long_rejects_when_low_stays_above_ema():
    s = _build({"pullback_proximity_pct": 0.05})
    # EMA = 100. All bar lows are 100.5+ (~0.5% above) — no real touch.
    bars = [
        _c(0,         100.6, 101.0, 100.5, 100.8, 100),
        _c(MS_30M,    100.8, 101.2, 100.6, 101.0, 100),
        _c(2 * MS_30M, 101.0, 101.5, 100.7, 101.3, 100),
    ]
    assert s._pullback_ok(_ind(bars, ema_now=100.0), is_long=True) is False


def test_pullback_long_accepts_when_low_touches_ema():
    s = _build({"pullback_proximity_pct": 0.05})
    # EMA = 100. Middle bar dips to 99.9 — actual pullback into the MA.
    bars = [
        _c(0,         100.6, 101.0, 100.5, 100.8, 100),
        _c(MS_30M,    100.8, 101.2, 99.9,  100.5, 100),
        _c(2 * MS_30M, 100.5, 101.5, 100.4, 101.3, 100),
    ]
    assert s._pullback_ok(_ind(bars, ema_now=100.0), is_long=True) is True


def test_pullback_long_accepts_via_vwap_when_ema_misses():
    s = _build({"pullback_proximity_pct": 0.05, "vwap_enabled": True})
    bars = [
        _c(0,         101.0, 101.5, 100.6, 101.0, 100),
        _c(MS_30M,    101.0, 101.2, 99.4,  100.7, 100),  # touches VWAP=99.5
        _c(2 * MS_30M, 100.7, 101.5, 100.6, 101.3, 100),
    ]
    # EMA far above lows so EMA branch fails; VWAP branch must catch it.
    assert s._pullback_ok(_ind(bars, ema_now=110.0, vwap_now=99.5), is_long=True) is True


def test_pullback_short_rejects_when_high_stays_below_ema():
    s = _build({"pullback_proximity_pct": 0.05})
    # EMA = 100. All highs are ~99.5 — no upward retrace into the MA.
    bars = [
        _c(0,         99.3, 99.5, 98.8, 99.0, 100),
        _c(MS_30M,    99.0, 99.4, 98.7, 98.9, 100),
        _c(2 * MS_30M, 98.9, 99.5, 98.5, 99.0, 100),
    ]
    assert s._pullback_ok(_ind(bars, ema_now=100.0), is_long=False) is False


def test_pullback_short_accepts_when_high_touches_ema():
    s = _build({"pullback_proximity_pct": 0.05})
    # EMA = 100. Middle bar wicks up to 100.1 — actual retrace into the MA.
    bars = [
        _c(0,         99.3, 99.5, 98.8, 99.0, 100),
        _c(MS_30M,    99.0, 100.1, 98.7, 99.2, 100),
        _c(2 * MS_30M, 99.2, 99.5, 98.5, 99.0, 100),
    ]
    assert s._pullback_ok(_ind(bars, ema_now=100.0), is_long=False) is True


# ---------------------------------------------------------------------------
# Trend invalidation
# ---------------------------------------------------------------------------

def _make_mp(side: str = "LONG", entry: float = 100.0, atr_val: float = 1.0) -> _ManagedPosition:
    return _ManagedPosition(
        symbol="BTCUSDT", side=side, entry_price=D(str(entry)),
        entry_atr=atr_val, r_distance=1.5 * atr_val, initial_qty=D("1"),
        entry_candle_open_time=0, highest_close=entry, lowest_close=entry,
    )


def test_invalidation_structure_break_long():
    s = _build({"invalidation_structure_lookback": 10})
    # Build 20 candles where the lookback low is 100, then a final close at 95
    candles = [_c(i * MS_30M, 102, 103, 100, 102, 100) for i in range(20)]
    candles.append(_c(20 * MS_30M, 95, 96, 94, 95, 100))
    mp = _make_mp(side="LONG", entry=102.0)
    assert s._check_trend_invalidation(mp, candles) is True


def test_invalidation_no_break_when_close_holds():
    s = _build({"invalidation_structure_lookback": 10})
    candles = [_c(i * MS_30M, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + i * 0.1, 100)
               for i in range(40)]
    mp = _make_mp(side="LONG", entry=100.0)
    # Close still above prior 10-bar low. May or may not invalidate via other branches —
    # we just assert it doesn't false-positive on structure break alone.
    # With a smooth uptrend, none of the four sub-conditions should fire.
    assert s._check_trend_invalidation(mp, candles) is False


def test_invalidation_slope_flip_long():
    s = _build({
        "ema_fast": 5, "slope_lookback": 2,
        "invalidation_structure_lookback": 100,
        "invalidation_strong_close_atr_mult": 100,
        "invalidation_momentum_adx_drop": 100,
    })
    # 30 rising then 5 falling — slope of EMA5 should flip
    candles = [_c(i * MS_30M, 100 + i, 101 + i, 99 + i, 100 + i, 100) for i in range(30)]
    for i in range(30, 35):
        candles.append(_c(i * MS_30M, 130 - (i - 29), 130 - (i - 29) + 1,
                          130 - (i - 29) - 1, 130 - (i - 29), 100))
    mp = _make_mp(side="LONG", entry=130.0)
    assert s._check_trend_invalidation(mp, candles) is True


# ---------------------------------------------------------------------------
# Dead-trade gate
# ---------------------------------------------------------------------------

def test_dead_trade_skipped_before_min_candles():
    s = _build({"dead_trade_min_candles": 24})
    candles = [_c(i * MS_30M, 100, 101, 99, 100, 100) for i in range(40)]
    mp = _make_mp()
    # Only 10 candles since entry → must NOT exit even if everything else true
    assert s._check_dead_trade(mp, candles, candles_since_entry=10) is False


def test_dead_trade_fires_when_all_three_true():
    s = _build({
        "dead_trade_min_candles": 5,
        "dead_trade_adx_lookback": 10,       # compare to ADX 10 bars ago for a clear decay
        "dead_trade_adx_floor": 100,         # ADX < 100 always true → easy pass
        "dead_trade_r_floor": 1.0,
        "atr_period": 5, "atr_sma_period": 5, "adx_period": 5,
    })
    # Phase 1: directional uptrend (bars 0..24) — ATR and ADX both rise.
    # Phase 2: balanced chop (bars 25..49) — both decay.
    candles = []
    for i in range(25):
        price = 100.0 + i
        candles.append(_c(i * MS_30M, price, price + 1, price - 1, price + 1, 100))
    for j in range(25):
        # Alternating up/down so -DM gets activity and ADX decays away from 100.
        if j % 2 == 0:
            candles.append(_c((25 + j) * MS_30M, 125.0, 125.2, 124.8, 125.1, 100))
        else:
            candles.append(_c((25 + j) * MS_30M, 125.1, 125.15, 124.7, 124.9, 100))

    # Trade entered late, sitting flat at ~125.
    mp = _make_mp(side="LONG", entry=125.0, atr_val=2.0)  # 1R = 3.0
    assert s._check_dead_trade(mp, candles, candles_since_entry=20) is True


def test_dead_trade_blocked_when_pnl_above_r():
    s = _build({
        "dead_trade_min_candles": 5,
        "dead_trade_adx_lookback": 3,
        "dead_trade_adx_floor": 100,
        "dead_trade_r_floor": 1.0,
        "atr_period": 5, "atr_sma_period": 5, "adx_period": 5,
    })
    candles = []
    for i in range(40):
        rng = max(1.0, 5.0 - i * 0.1)
        candles.append(_c(i * MS_30M, 100, 100 + rng, 100 - rng, 100, 100))
    # entry 100 with R=1.5; last close 110 → +10 per unit, way above 1R
    candles[-1] = _c(39 * MS_30M, 110, 111, 109, 110, 100)
    mp = _make_mp(side="LONG", entry=100.0, atr_val=1.0)
    assert s._check_dead_trade(mp, candles, candles_since_entry=30) is False


# ---------------------------------------------------------------------------
# Sync managed state
# ---------------------------------------------------------------------------

def test_sync_clears_managed_when_position_gone():
    s = _build()
    s._managed["BTCUSDT"] = _make_mp()
    s.state_manager.get_state.return_value = _state(position=Position.NONE)
    s._sync_managed("BTCUSDT")
    assert "BTCUSDT" not in s._managed


def test_sync_keeps_managed_when_position_still_open():
    s = _build()
    s._managed["BTCUSDT"] = _make_mp()
    s.state_manager.get_state.return_value = _state(position=Position.LONG, size=D("1"))
    s._sync_managed("BTCUSDT")
    assert "BTCUSDT" in s._managed


# ---------------------------------------------------------------------------
# Persistence: serialize_state + adopt
# ---------------------------------------------------------------------------

def test_serialize_state_returns_managed_fields():
    s = _build()
    mp = _make_mp(side="LONG", entry=100.0, atr_val=2.0)
    mp.highest_close = 110.0
    mp.lowest_close = 95.0
    mp.entry_candle_open_time = 123456
    s._managed["BTCUSDT"] = mp
    blob = s.serialize_state("BTCUSDT")
    assert blob["entry_atr"] == 2.0
    assert blob["r_distance"] == 3.0  # 1.5 * 2.0
    assert blob["highest_close"] == 110.0
    assert blob["lowest_close"] == 95.0
    assert blob["entry_candle_open_time"] == 123456


def test_serialize_state_empty_when_unmanaged():
    s = _build()
    assert s.serialize_state("BTCUSDT") == {}


def test_adopt_skips_when_no_position():
    s = _build()
    s.state_manager.get_state.return_value = _state(position=Position.NONE)
    entry = {
        "strategy": "adaptive_trend_pullback", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 3.0,
                           "highest_close": 100.0, "lowest_close": 100.0,
                           "entry_candle_open_time": 0},
        "orders": {"stop_loss_id": 7, "tp1_id": 8},
    }
    s.adopt("BTCUSDT", entry)
    assert "BTCUSDT" not in s._managed


def test_adopt_rehydrates_managed_position_when_orders_alive():
    s = _build()
    live_state = _state(position=Position.LONG, size=D("1"))
    live_state.orders = [
        {"order_id": 7, "side": "SELL", "is_algo": True, "order_type": "STOP_MARKET",
         "stop_price": D("97")},
        {"order_id": 8, "side": "SELL", "is_algo": False, "order_type": "LIMIT",
         "price": D("103")},
    ]
    s.state_manager.get_state.return_value = live_state
    entry = {
        "strategy": "adaptive_trend_pullback", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 3.0,
                           "highest_close": 108.0, "lowest_close": 100.0,
                           "entry_candle_open_time": 42},
        "orders": {"stop_loss_id": 7, "tp1_id": 8},
    }
    s.adopt("BTCUSDT", entry)
    mp = s._managed["BTCUSDT"]
    assert mp.side == "LONG"
    assert mp.entry_price == D("100")
    assert mp.entry_atr == 2.0
    assert mp.r_distance == 3.0
    assert mp.highest_close == 108.0
    assert mp.entry_candle_open_time == 42
    assert mp.current_stop_order_id == 7
    assert mp.tp1_order_id == 8


def test_adopt_replaces_missing_stop_long():
    s = _build()
    live_state = _state(position=Position.LONG, size=D("1"))
    live_state.orders = []  # no SL on Binance
    s.state_manager.get_state.return_value = live_state
    entry = {
        "strategy": "adaptive_trend_pullback", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 3.0,
                           "highest_close": 100.0, "lowest_close": 100.0,
                           "entry_candle_open_time": 0},
        "orders": {"stop_loss_id": 99, "tp1_id": None},
    }
    with patch("core.strategies.adaptive_trend_pullback.algo_orders.place_stop_market_order",
               return_value={"order_id": 123}) as place_stop:
        s.adopt("BTCUSDT", entry)
        place_stop.assert_called_once()
        args, kwargs = place_stop.call_args
        # symbol, side, qty, stop_price — entry - r_distance = 97
        assert args[1] == "BTCUSDT"
        assert args[2] == "SELL"
        assert args[3] == D("1")
        assert args[4] == D("97.0")
    mp = s._managed["BTCUSDT"]
    assert mp.current_stop_order_id == 123
    assert mp.tp1_order_id is None


def test_adopt_replaces_missing_stop_short():
    s = _build()
    live_state = _state(position=Position.SHORT, size=D("1"))
    live_state.orders = []
    s.state_manager.get_state.return_value = live_state
    entry = {
        "strategy": "adaptive_trend_pullback", "side": "SHORT",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 3.0,
                           "highest_close": 100.0, "lowest_close": 100.0,
                           "entry_candle_open_time": 0},
        "orders": {"stop_loss_id": 99, "tp1_id": None},
    }
    with patch("core.strategies.adaptive_trend_pullback.algo_orders.place_stop_market_order",
               return_value={"order_id": 456}) as place_stop:
        s.adopt("BTCUSDT", entry)
        args, _ = place_stop.call_args
        # short: stop = entry + r_distance = 103, exit side BUY
        assert args[2] == "BUY"
        assert args[4] == D("103.0")
    assert s._managed["BTCUSDT"].current_stop_order_id == 456


def test_adopt_pre_existing_only_adopts_own_entries():
    s = _build()
    s.state_manager.get_state.return_value = _state(position=Position.LONG, size=D("1"))
    s.state_manager.get_owner.side_effect = lambda sym: {
        "BTCUSDT": {
            "strategy": "adaptive_trend_pullback", "side": "LONG",
            "entry_price": "100", "qty": "1",
            "strategy_state": {"entry_atr": 2.0, "r_distance": 3.0,
                               "highest_close": 100.0, "lowest_close": 100.0,
                               "entry_candle_open_time": 0},
            "orders": {"stop_loss_id": None, "tp1_id": None},
        },
    }.get(sym)
    with patch("core.strategies.adaptive_trend_pullback.algo_orders.place_stop_market_order",
               return_value={"order_id": 999}):
        s.adopt_pre_existing()
    assert "BTCUSDT" in s._managed


def test_first_failed_long_gate_returns_none_when_all_pass():
    s = _build({"pullback_proximity_pct": 100.0, "adx_min": 0, "rsi_max_long": 101})
    ind = _EntryIndicators(
        candles=[], close=130.5, prev_close=129.5, open_=129.5, volume=10000,
        ema_now=128.0, atr_now=2.0, atr_sma=1.0, adx_now=30.0, rsi_now=50.0,
        vol_sma=200, vwap_now=None,
        pullback_bars=[_c(0, 120, 121, 119.0, 120, 100)],
        pullback_high=121.0, pullback_low=119.0,
    )
    assert s._first_failed_long_gate(ind) is None


def test_first_failed_long_gate_reports_volume_when_volume_low():
    s = _build({"pullback_proximity_pct": 100.0, "adx_min": 0, "rsi_max_long": 101})
    # pullback OK (proximity 100%), bullish OK, higher OK, then volume fails
    ind = _EntryIndicators(
        candles=[], close=130.5, prev_close=129.5, open_=129.5, volume=10,
        ema_now=128.0, atr_now=2.0, atr_sma=1.0, adx_now=30.0, rsi_now=50.0,
        vol_sma=200, vwap_now=None,
        pullback_bars=[_c(0, 120, 121, 119.0, 120, 100)],
        pullback_high=121.0, pullback_low=119.0,
    )
    fail = s._first_failed_long_gate(ind)
    assert fail is not None
    assert fail[0] == "volume"


def test_first_failed_long_gate_short_circuits_before_volume():
    s = _build({"pullback_proximity_pct": 100.0, "adx_min": 0, "rsi_max_long": 101})
    # Bullish gate fails first (close <= open), so volume isn't even inspected.
    ind = _EntryIndicators(
        candles=[], close=129.0, prev_close=128.0, open_=130.0, volume=10,
        ema_now=128.0, atr_now=2.0, atr_sma=1.0, adx_now=30.0, rsi_now=50.0,
        vol_sma=200, vwap_now=None,
        pullback_bars=[_c(0, 120, 121, 119.0, 120, 100)],
        pullback_high=121.0, pullback_low=119.0,
    )
    fail = s._first_failed_long_gate(ind)
    assert fail is not None
    assert fail[0] == "bullish_close"  # not "volume" — short-circuit worked


def test_regime_summary_returns_diagnostic_string():
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = [
        _c(i * MS_4H, 100 + i, 101 + i, 99 + i, 100 + i, 10) for i in range(50)
    ]
    long_ok, short_ok, summary = s._regime_summary("BTCUSDT")
    assert long_ok is True
    assert short_ok is False
    assert "close=" in summary
    assert "slope=" in summary


def test_regime_summary_warmup_reports_candle_count():
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = [_c(i * MS_4H, 100, 101, 99, 100, 10) for i in range(5)]
    long_ok, short_ok, summary = s._regime_summary("BTCUSDT")
    assert (long_ok, short_ok) == (False, False)
    assert "warmup" in summary
    assert "5/" in summary


def test_adopt_pre_existing_ignores_other_strategy_entries():
    s = _build()
    s.state_manager.get_state.return_value = _state(position=Position.LONG, size=D("1"))
    s.state_manager.get_owner.side_effect = lambda sym: {
        "BTCUSDT": {
            "strategy": "different_strategy", "side": "LONG",
            "entry_price": "100", "qty": "1",
            "strategy_state": {}, "orders": {},
        },
    }.get(sym)
    s.adopt_pre_existing()
    assert "BTCUSDT" not in s._managed
