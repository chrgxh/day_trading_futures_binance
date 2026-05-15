"""Unit tests for BBRsiMeanReversion.

Focus on the deterministic decision surfaces:
- regime gate (range vs trend)
- entry-gate compositing
- streak updates + middle-touch
- invalidation truth table
- time exit
- adopt / serialize for restart recovery

Execution paths (IOC placement, exit close) are skipped — they are I/O-bound
and covered by integration tests against the testnet.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from core.strategies.bb_rsi_mean_reversion import (
    BBRsiMeanReversion,
    _EntryIndicators,
    _ManagedPosition,
)
from core.types import Action, Position, SymbolState


D = Decimal
MS_30M = 1_800_000
MS_4H = 14_400_000
MS_1D = 86_400_000


def _c(t: int, o: float, h: float, l: float, c: float, v: float) -> dict:
    return {"open_time": t, "open": D(str(o)), "high": D(str(h)),
            "low": D(str(l)), "close": D(str(c)), "volume": D(str(v))}


def _state(symbol="BTCUSDT", position=Position.NONE, size=D("0")) -> SymbolState:
    return SymbolState(symbol=symbol, position=position, size=size,
                       entry_price=D("100"), mark_price=D("100"),
                       unrealized_pnl=D("0"), orders=[])


def _build(params: Optional[dict] = None, has_position=False) -> BBRsiMeanReversion:
    """Build a BBRsiMeanReversion wired to mocks. Override params via the arg."""
    defaults = {
        "entry_interval": "30m",
        "regime_interval": "4h",
        "macro_interval": "1d",
        "leverage": 5,
        "notional_per_trade_usdt": 100,

        "macro_ema_fast": 5,
        "macro_ema_slow": 10,

        "regime_adx_period": 5,
        "regime_adx_max_range": 20.0,
        "regime_adx_min_trend": 25.0,
        "regime_ema_fast": 5,
        "regime_ema_slow": 10,
        "regime_ema_flatness_pct": 2.0,

        "bb_period": 10,
        "bb_num_std": 2.0,
        "rsi_period": 5,
        "rsi_oversold": 30.0,
        "rsi_overbought": 70.0,
        "atr_period": 5,
        "atr_sma_period": 5,
        "volume_sma_period": 5,

        "pierce_lookback": 2,
        "volume_max_mult": 1.5,
        "max_pierce_atr_mult": 1.0,
        "atr_max_expansion_mult": 1.2,

        "stop_atr_mult": 1.0,
        "structure_stop_lookback": 5,
        "structure_stop_buffer_atr_mult": 0.1,
        "tp1_size_pct": 0.7,
        "tp2_size_pct": 0.3,
        "break_even_offset_atr_mult": 0.0,

        "max_outside_band_candles": 2,
        "max_rsi_extreme_candles": 2,

        "time_exit_soft_candles": 8,
        "time_exit_hard_candles": 16,
    }
    if params:
        defaults.update(params)

    sm = MagicMock()
    sm.has_position.return_value = has_position
    sm.get_state.return_value = _state()
    rg = MagicMock()
    rg.allow_open.return_value = True

    s = BBRsiMeanReversion(
        name="bb_rsi_mean_reversion",
        symbols=["BTCUSDT"],
        params=defaults,
        client=MagicMock(),
        sym_infos={"BTCUSDT": {"tick_size": D("0.1"), "step_size": D("0.001")}},
        state_manager=sm,
        risk_guard=rg,
        live_trade_manager=None,
    )
    return s


def _ind(**overrides) -> _EntryIndicators:
    """Build an _EntryIndicators with sensible defaults — override fields by kw.

    Default `swing_low` is set far below `bb_lower` and `swing_high` far above
    `bb_upper` so the structure stop never ends up tighter than the ATR stop in
    tests that don't explicitly exercise it. Tests for the structure-stop logic
    pass tighter swings via overrides.
    """
    defaults = dict(
        candles=[], close=99.0, prev_close=98.5, open_=98.7, low=98.0, high=99.5,
        prev_low=97.5, prev_high=99.0, volume=100.0,
        bb_upper=105.0, bb_middle=100.0, bb_lower=98.5,
        atr_now=1.0, atr_sma=1.0, rsi_now=25.0, vol_sma=120.0,
        swing_low=50.0, swing_high=200.0,
    )
    defaults.update(overrides)
    return _EntryIndicators(**defaults)


# ---------------------------------------------------------------------------
# Constructor + intervals
# ---------------------------------------------------------------------------

def test_intervals_derived_from_params():
    s = _build({"entry_interval": "15m", "regime_interval": "1h", "macro_interval": "4h"})
    assert s.intervals == ["15m", "1h", "4h"]


def test_intervals_deduplicated_when_intervals_overlap():
    s = _build({"entry_interval": "15m", "regime_interval": "1h", "macro_interval": "1h"})
    assert s.intervals == ["15m", "1h"]


def test_4h_candle_only_buffers_no_action():
    s = _build()
    s._buffers["BTCUSDT"]["30m"] = []
    s._buffers["BTCUSDT"]["4h"] = [_c(i * MS_4H, 100, 101, 99, 100, 10) for i in range(50)]
    s.on_candle("BTCUSDT", "4h", _c(50 * MS_4H, 100, 101, 99, 100, 10))
    assert "BTCUSDT" not in s._managed


# ---------------------------------------------------------------------------
# Regime gate
# ---------------------------------------------------------------------------

def test_regime_rejects_when_trending_uptrend():
    s = _build()
    # Strong uptrend → high ADX → trending
    s._buffers["BTCUSDT"]["4h"] = [
        _c(i * MS_4H, 100 + i, 101 + i, 99 + i, 100 + i, 10) for i in range(60)
    ]
    ok, summary = s._regime_summary("BTCUSDT")
    assert ok is False


def test_regime_accepts_when_flat_and_low_adx():
    s = _build()
    # Choppy flat market: small zigzags, no directional bias → low ADX, flat EMAs
    bars = []
    for i in range(60):
        bias = 100.0 + ((i % 4) - 1.5) * 0.05
        bars.append(_c(i * MS_4H, bias, bias + 0.2, bias - 0.2, bias, 10))
    s._buffers["BTCUSDT"]["4h"] = bars
    ok, summary = s._regime_summary("BTCUSDT")
    assert ok is True
    assert "range" in summary


def test_regime_rejects_during_warmup():
    s = _build()
    s._buffers["BTCUSDT"]["4h"] = [_c(i * MS_4H, 100, 101, 99, 100, 10) for i in range(3)]
    ok, summary = s._regime_summary("BTCUSDT")
    assert ok is False
    assert "warmup" in summary


# ---------------------------------------------------------------------------
# Entry gate compositing
# ---------------------------------------------------------------------------

def test_long_gate_passes_with_clean_setup():
    s = _build()
    # Pierce: current bar low went below bb_lower (via candles in buffer)
    s._buffers["BTCUSDT"]["30m"] = [_c(0, 100, 101, 97.0, 99.0, 100)]  # current low pierces
    ind = _ind(close=99.0, prev_close=98.5, open_=98.7, low=97.0,
               bb_lower=98.5, rsi_now=25.0, volume=100.0, vol_sma=200.0,
               atr_now=1.0, atr_sma=1.0)
    assert s._first_failed_long_gate("BTCUSDT", ind) is None


def test_long_gate_fails_no_pierce():
    s = _build()
    s._buffers["BTCUSDT"]["30m"] = [_c(0, 100, 101, 99, 100, 100)]
    ind = _ind(close=99.0, low=99.0, bb_lower=98.0)  # no pierce
    fail = s._first_failed_long_gate("BTCUSDT", ind)
    assert fail is not None and fail[0] == "no_pierce"


def test_long_gate_fails_rsi_not_oversold():
    s = _build()
    s._buffers["BTCUSDT"]["30m"] = [_c(0, 100, 101, 97.0, 99.0, 100)]
    ind = _ind(close=99.0, low=97.0, bb_lower=98.5, rsi_now=50.0)
    fail = s._first_failed_long_gate("BTCUSDT", ind)
    assert fail is not None and fail[0] == "rsi_not_oversold"


def test_long_gate_fails_volume_spike():
    s = _build()
    s._buffers["BTCUSDT"]["30m"] = [_c(0, 100, 101, 97.0, 99.0, 100)]
    ind = _ind(close=99.0, prev_close=98.5, open_=98.7, low=97.0,
               bb_lower=98.5, rsi_now=25.0,
               volume=500.0, vol_sma=100.0)  # 5x SMA → fail
    fail = s._first_failed_long_gate("BTCUSDT", ind)
    assert fail is not None and fail[0] == "volume_spike"


def test_long_gate_fails_pierce_too_deep():
    s = _build()
    s._buffers["BTCUSDT"]["30m"] = [_c(0, 100, 101, 90.0, 95.0, 100)]
    ind = _ind(close=95.0, prev_close=98.5, open_=94.0, low=90.0,
               bb_lower=98.5, rsi_now=25.0, atr_now=1.0,
               volume=100.0, vol_sma=200.0)
    # gap = 98.5 - 95.0 = 3.5, ATR=1.0, max_pierce_atr_mult=1.0 → too deep.
    # But: bullish needs close > open, close > prev_close, AND close > bb_lower (reclaim)
    # Here close=95 < bb_lower=98.5 → no_reclaim fails first.
    fail = s._first_failed_long_gate("BTCUSDT", ind)
    assert fail is not None and fail[0] == "no_reclaim"


def test_long_gate_fails_atr_expanding():
    s = _build()
    s._buffers["BTCUSDT"]["30m"] = [_c(0, 100, 101, 97.0, 99.0, 100)]
    ind = _ind(close=99.0, prev_close=98.5, open_=98.7, low=97.0,
               bb_lower=98.5, rsi_now=25.0,
               atr_now=5.0, atr_sma=1.0,  # 5x SMA → expanding
               volume=100.0, vol_sma=200.0)
    fail = s._first_failed_long_gate("BTCUSDT", ind)
    assert fail is not None and fail[0] == "atr_expanding"


def test_short_gate_passes_with_clean_setup():
    s = _build()
    s._buffers["BTCUSDT"]["30m"] = [_c(0, 100, 103.0, 99, 101.0, 100)]  # current high pierces upper
    ind = _ind(close=101.0, prev_close=101.5, open_=101.3, high=103.0,
               bb_upper=102.5, rsi_now=75.0, volume=100.0, vol_sma=200.0,
               atr_now=1.0, atr_sma=1.0)
    assert s._first_failed_short_gate("BTCUSDT", ind) is None


# ---------------------------------------------------------------------------
# Pierce detection
# ---------------------------------------------------------------------------

def test_pierce_lower_via_current_low():
    s = _build({"pierce_lookback": 2})
    s._buffers["BTCUSDT"]["30m"] = [
        _c(0, 100, 101, 99.5, 100, 100),       # prev: above band
        _c(MS_30M, 100, 100.5, 97.0, 99.0, 100),  # current: low pierces
    ]
    ind = _ind(bb_lower=98.5)
    assert s._pierced_lower("BTCUSDT", ind) is True


def test_pierce_lower_via_prev_close():
    s = _build({"pierce_lookback": 2})
    s._buffers["BTCUSDT"]["30m"] = [
        _c(0, 100, 101, 98.0, 98.2, 100),       # prev: close pierces (98.2 < 98.5)
        _c(MS_30M, 99, 99.5, 99.0, 99.3, 100),  # current: well above
    ]
    ind = _ind(bb_lower=98.5)
    assert s._pierced_lower("BTCUSDT", ind) is True


def test_pierce_lower_no_pierce_when_all_above():
    s = _build({"pierce_lookback": 2})
    s._buffers["BTCUSDT"]["30m"] = [
        _c(0, 100, 101, 99.5, 100.0, 100),
        _c(MS_30M, 100, 100.5, 99.6, 100.2, 100),
    ]
    ind = _ind(bb_lower=98.5)
    assert s._pierced_lower("BTCUSDT", ind) is False


def test_pierce_upper_symmetric():
    s = _build({"pierce_lookback": 2})
    s._buffers["BTCUSDT"]["30m"] = [
        _c(0, 100, 100.5, 99.5, 100.0, 100),
        _c(MS_30M, 100.5, 103.0, 100.0, 101.0, 100),  # current high > bb_upper
    ]
    ind = _ind(bb_upper=102.5)
    assert s._pierced_upper("BTCUSDT", ind) is True


# ---------------------------------------------------------------------------
# Streaks + middle-touch updates
# ---------------------------------------------------------------------------

def _make_mp(side: str = "LONG", entry: float = 100.0, atr_val: float = 1.0,
             **overrides) -> _ManagedPosition:
    mp = _ManagedPosition(
        symbol="BTCUSDT", side=side, entry_price=D(str(entry)),
        entry_atr=atr_val, r_distance=1.0 * atr_val, initial_qty=D("1"),
        entry_candle_open_time=0,
    )
    for k, v in overrides.items():
        setattr(mp, k, v)
    return mp


def test_streak_long_close_below_band_increments():
    s = _build()
    mp = _make_mp(side="LONG")
    ind = _ind(close=97.0, bb_lower=98.5, rsi_now=25.0)
    s._update_streaks_and_touch(mp, ind)
    assert mp.outside_band_streak == 1
    s._update_streaks_and_touch(mp, ind)
    assert mp.outside_band_streak == 2


def test_streak_long_close_above_band_resets():
    s = _build()
    mp = _make_mp(side="LONG", outside_band_streak=3)
    ind = _ind(close=99.0, bb_lower=98.5)
    s._update_streaks_and_touch(mp, ind)
    assert mp.outside_band_streak == 0


def test_streak_rsi_extreme_long():
    s = _build()
    mp = _make_mp(side="LONG")
    ind = _ind(close=99.0, bb_lower=98.5, rsi_now=25.0)  # below 30 → streak ++
    s._update_streaks_and_touch(mp, ind)
    assert mp.rsi_extreme_streak == 1


def test_streak_rsi_extreme_short():
    s = _build()
    mp = _make_mp(side="SHORT")
    ind = _ind(close=101.0, bb_upper=102.5, rsi_now=75.0)  # above 70 → streak ++
    s._update_streaks_and_touch(mp, ind)
    assert mp.rsi_extreme_streak == 1


def test_middle_touch_long_set_when_close_reaches_mid():
    s = _build()
    mp = _make_mp(side="LONG")
    ind = _ind(close=100.0, bb_middle=100.0, bb_lower=98.5)
    s._update_streaks_and_touch(mp, ind)
    assert mp.touched_middle is True


def test_middle_touch_short_set_when_close_reaches_mid():
    s = _build()
    mp = _make_mp(side="SHORT")
    ind = _ind(close=100.0, bb_middle=100.0, bb_upper=102.5)
    s._update_streaks_and_touch(mp, ind)
    assert mp.touched_middle is True


def test_middle_touch_sticky():
    s = _build()
    mp = _make_mp(side="LONG", touched_middle=True)
    ind = _ind(close=98.6, bb_middle=100.0, bb_lower=98.5)  # back below mid
    s._update_streaks_and_touch(mp, ind)
    assert mp.touched_middle is True


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------

def test_invalidation_4h_adx_turns_trending():
    s = _build({"regime_adx_min_trend": 25.0})
    mp = _make_mp(side="LONG")
    ind = _ind(close=99.0, bb_lower=98.5, atr_now=1.0, atr_sma=1.0, rsi_now=50.0)
    with patch.object(s, "_regime_adx_now", return_value=30.0):
        reason = s._check_trend_invalidation(mp, ind, "BTCUSDT")
    assert reason is not None and "regime trending" in reason


def test_invalidation_atr_expansion():
    s = _build({"atr_max_expansion_mult": 1.2})
    mp = _make_mp(side="LONG")
    ind = _ind(close=99.0, bb_lower=98.5, atr_now=5.0, atr_sma=1.0, rsi_now=50.0)
    with patch.object(s, "_regime_adx_now", return_value=15.0):
        reason = s._check_trend_invalidation(mp, ind, "BTCUSDT")
    assert reason is not None and "ATR expanding" in reason


def test_invalidation_outside_band_streak():
    s = _build({"max_outside_band_candles": 2})
    mp = _make_mp(side="LONG", outside_band_streak=2)
    ind = _ind(close=99.0, bb_lower=98.5, atr_now=1.0, atr_sma=1.0, rsi_now=50.0)
    with patch.object(s, "_regime_adx_now", return_value=15.0):
        reason = s._check_trend_invalidation(mp, ind, "BTCUSDT")
    assert reason is not None and "outside band" in reason


def test_invalidation_rsi_streak():
    s = _build({"max_rsi_extreme_candles": 2})
    mp = _make_mp(side="LONG", rsi_extreme_streak=2)
    ind = _ind(close=99.0, bb_lower=98.5, atr_now=1.0, atr_sma=1.0, rsi_now=50.0)
    with patch.object(s, "_regime_adx_now", return_value=15.0):
        reason = s._check_trend_invalidation(mp, ind, "BTCUSDT")
    assert reason is not None and "RSI extreme streak" in reason


def test_invalidation_close_below_hard_sl_long():
    s = _build({"stop_atr_mult": 1.0})
    mp = _make_mp(side="LONG", entry=100.0, atr_val=2.0)  # SL at 98.0
    ind = _ind(close=97.0, bb_lower=96.0, atr_now=1.0, atr_sma=1.0, rsi_now=50.0)
    with patch.object(s, "_regime_adx_now", return_value=15.0):
        reason = s._check_trend_invalidation(mp, ind, "BTCUSDT")
    assert reason is not None and "below SL" in reason


def test_invalidation_quiet_when_all_ok():
    s = _build()
    mp = _make_mp(side="LONG", entry=100.0, atr_val=1.0)
    ind = _ind(close=99.5, bb_lower=98.5, atr_now=1.0, atr_sma=1.0, rsi_now=50.0)
    with patch.object(s, "_regime_adx_now", return_value=15.0):
        reason = s._check_trend_invalidation(mp, ind, "BTCUSDT")
    assert reason is None


# ---------------------------------------------------------------------------
# Time exit
# ---------------------------------------------------------------------------

def test_time_exit_hard_max_fires_unconditionally():
    s = _build({"time_exit_hard_candles": 16, "time_exit_soft_candles": 8})
    mp = _make_mp(side="LONG", touched_middle=True)  # even touched
    ind = _ind(close=99.0)
    reason = s._check_time_exit(mp, ind, candles_since_entry=20)
    assert reason is not None and "hard time exit" in reason


def test_time_exit_soft_blocked_when_touched_middle():
    s = _build({"time_exit_soft_candles": 8, "time_exit_hard_candles": 100})
    mp = _make_mp(side="LONG", entry=100.0, touched_middle=True)
    ind = _ind(close=99.0)   # below entry, BUT touched-middle is sticky → no exit
    reason = s._check_time_exit(mp, ind, candles_since_entry=10)
    assert reason is None


def test_time_exit_soft_blocked_when_close_above_entry_long():
    s = _build({"time_exit_soft_candles": 8, "time_exit_hard_candles": 100})
    mp = _make_mp(side="LONG", entry=100.0)
    ind = _ind(close=100.5)  # right side of entry → trade still working
    reason = s._check_time_exit(mp, ind, candles_since_entry=10)
    assert reason is None


def test_time_exit_soft_fires_when_close_below_entry_long():
    s = _build({"time_exit_soft_candles": 8, "time_exit_hard_candles": 100})
    mp = _make_mp(side="LONG", entry=100.0)
    ind = _ind(close=99.5)   # wrong side of entry, no mid-touch → soft exit
    reason = s._check_time_exit(mp, ind, candles_since_entry=10)
    assert reason is not None and "soft time exit" in reason


def test_time_exit_soft_fires_when_close_above_entry_short():
    s = _build({"time_exit_soft_candles": 8, "time_exit_hard_candles": 100})
    mp = _make_mp(side="SHORT", entry=100.0)
    ind = _ind(close=100.5)  # wrong side of entry for SHORT → soft exit
    reason = s._check_time_exit(mp, ind, candles_since_entry=10)
    assert reason is not None and "soft time exit" in reason


def test_time_exit_soft_skipped_before_soft_max():
    s = _build({"time_exit_soft_candles": 8, "time_exit_hard_candles": 100})
    mp = _make_mp(side="LONG", entry=100.0)
    ind = _ind(close=99.5)   # wrong side, but only 5 candles in
    reason = s._check_time_exit(mp, ind, candles_since_entry=5)
    assert reason is None


# ---------------------------------------------------------------------------
# Sync managed
# ---------------------------------------------------------------------------

def test_sync_clears_managed_when_position_gone():
    s = _build()
    s._managed["BTCUSDT"] = _make_mp()
    s.state_manager.get_state.return_value = _state(position=Position.NONE)
    s._sync_managed("BTCUSDT")
    assert "BTCUSDT" not in s._managed


def test_sync_keeps_managed_when_position_open():
    s = _build()
    s._managed["BTCUSDT"] = _make_mp()
    s.state_manager.get_state.return_value = _state(position=Position.LONG, size=D("1"))
    s._sync_managed("BTCUSDT")
    assert "BTCUSDT" in s._managed


# ---------------------------------------------------------------------------
# Persistence (serialize / adopt)
# ---------------------------------------------------------------------------

def test_serialize_state_returns_managed_fields():
    s = _build()
    mp = _make_mp(side="LONG", entry=100.0, atr_val=2.0,
                  outside_band_streak=1, rsi_extreme_streak=2, touched_middle=True,
                  entry_candle_open_time=12345)
    s._managed["BTCUSDT"] = mp
    blob = s.serialize_state("BTCUSDT")
    assert blob["entry_atr"] == 2.0
    assert blob["r_distance"] == 2.0
    assert blob["outside_band_streak"] == 1
    assert blob["rsi_extreme_streak"] == 2
    assert blob["touched_middle"] is True
    assert blob["entry_candle_open_time"] == 12345


def test_serialize_state_empty_when_unmanaged():
    s = _build()
    assert s.serialize_state("BTCUSDT") == {}


def test_adopt_skips_when_no_position():
    s = _build()
    s.state_manager.get_state.return_value = _state(position=Position.NONE)
    entry = {
        "strategy": "bb_rsi_mean_reversion", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 1.0, "r_distance": 1.0,
                           "outside_band_streak": 0, "rsi_extreme_streak": 0,
                           "touched_middle": False, "entry_candle_open_time": 0},
        "orders": {"stop_loss_id": 7, "tp1_id": 8, "tp2_id": 9},
    }
    s.adopt("BTCUSDT", entry)
    assert "BTCUSDT" not in s._managed


def test_adopt_rehydrates_when_orders_alive():
    s = _build()
    live_state = _state(position=Position.LONG, size=D("1"))
    live_state.orders = [
        {"order_id": 7, "side": "SELL", "is_algo": True},
        {"order_id": 8, "side": "SELL", "is_algo": False},
        {"order_id": 9, "side": "SELL", "is_algo": False},
    ]
    s.state_manager.get_state.return_value = live_state
    entry = {
        "strategy": "bb_rsi_mean_reversion", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 2.0,
                           "outside_band_streak": 1, "rsi_extreme_streak": 0,
                           "touched_middle": True, "entry_candle_open_time": 42},
        "orders": {"stop_loss_id": 7, "tp1_id": 8, "tp2_id": 9},
    }
    s.adopt("BTCUSDT", entry)
    mp = s._managed["BTCUSDT"]
    assert mp.side == "LONG"
    assert mp.entry_price == D("100")
    assert mp.entry_atr == 2.0
    assert mp.r_distance == 2.0
    assert mp.outside_band_streak == 1
    assert mp.touched_middle is True
    assert mp.stop_loss_order_id == 7
    assert mp.tp1_order_id == 8
    assert mp.tp2_order_id == 9


def test_adopt_replaces_missing_stop_long():
    s = _build()
    live_state = _state(position=Position.LONG, size=D("1"))
    live_state.orders = []
    s.state_manager.get_state.return_value = live_state
    entry = {
        "strategy": "bb_rsi_mean_reversion", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 2.0,
                           "outside_band_streak": 0, "rsi_extreme_streak": 0,
                           "touched_middle": False, "entry_candle_open_time": 0},
        "orders": {"stop_loss_id": 99, "tp1_id": None, "tp2_id": None},
    }
    with patch("core.strategies.bb_rsi_mean_reversion.algo_orders.place_stop_market_order",
               return_value={"order_id": 321}) as place_stop:
        s.adopt("BTCUSDT", entry)
        args, _ = place_stop.call_args
        # symbol, side, qty, stop_price — entry - r_distance = 98
        assert args[1] == "BTCUSDT"
        assert args[2] == "SELL"
        assert args[3] == D("1")
        assert args[4] == D("98.0")
    mp = s._managed["BTCUSDT"]
    assert mp.stop_loss_order_id == 321
    assert mp.tp1_order_id is None
    assert mp.tp2_order_id is None


def test_adopt_replaces_missing_stop_short():
    s = _build()
    live_state = _state(position=Position.SHORT, size=D("1"))
    live_state.orders = []
    s.state_manager.get_state.return_value = live_state
    entry = {
        "strategy": "bb_rsi_mean_reversion", "side": "SHORT",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 2.0,
                           "outside_band_streak": 0, "rsi_extreme_streak": 0,
                           "touched_middle": False, "entry_candle_open_time": 0},
        "orders": {"stop_loss_id": 99, "tp1_id": None, "tp2_id": None},
    }
    with patch("core.strategies.bb_rsi_mean_reversion.algo_orders.place_stop_market_order",
               return_value={"order_id": 654}) as place_stop:
        s.adopt("BTCUSDT", entry)
        args, _ = place_stop.call_args
        assert args[2] == "BUY"
        assert args[4] == D("102.0")
    assert s._managed["BTCUSDT"].stop_loss_order_id == 654


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


# ---------------------------------------------------------------------------
# TP qty split
# ---------------------------------------------------------------------------

def test_split_tp_qty_70_30():
    s = _build({"tp1_size_pct": 0.7, "tp2_size_pct": 0.3})
    tp1, tp2 = s._split_tp_qty(D("1.000"), D("0.001"))
    assert tp1 == D("0.700")
    # TP2 takes the remainder so the two never exceed the filled qty.
    assert tp1 + tp2 <= D("1.000")
    assert tp2 == D("0.300")


def test_split_tp_qty_no_tp2():
    s = _build({"tp1_size_pct": 1.0, "tp2_size_pct": 0.0})
    tp1, tp2 = s._split_tp_qty(D("1.000"), D("0.001"))
    assert tp1 == D("1.000")
    assert tp2 == D("0")


# ---------------------------------------------------------------------------
# Structure stop — picks the more conservative (closer-to-entry) of ATR/swing
# ---------------------------------------------------------------------------

def test_compute_exit_prices_long_uses_structure_when_tighter():
    # ATR stop at 100 - 2 = 98; structure stop at 99 (tighter / closer to entry)
    s = _build({"stop_atr_mult": 1.0})
    stop_price, _tp1, _tp2, r_distance = s._compute_exit_prices(
        fill_price=D("100"), entry_atr=2.0, is_long=True,
        bb_middle=102.0, bb_opposite=104.0,
        structure_stop_price=99.0,                # closer to entry than ATR stop 98
        tick_size=D("0.1"),
    )
    assert stop_price == D("99.0")
    assert r_distance == pytest.approx(1.0)


def test_compute_exit_prices_long_uses_atr_when_tighter():
    # ATR stop at 100 - 2 = 98; structure stop at 95 (looser / further from entry)
    s = _build({"stop_atr_mult": 1.0})
    stop_price, _tp1, _tp2, r_distance = s._compute_exit_prices(
        fill_price=D("100"), entry_atr=2.0, is_long=True,
        bb_middle=102.0, bb_opposite=104.0,
        structure_stop_price=95.0,                # further from entry than ATR stop
        tick_size=D("0.1"),
    )
    assert stop_price == D("98.0")
    assert r_distance == pytest.approx(2.0)


def test_compute_exit_prices_short_uses_structure_when_tighter():
    # ATR stop at 100 + 2 = 102; structure stop at 101 (tighter / closer to entry)
    s = _build({"stop_atr_mult": 1.0})
    stop_price, _tp1, _tp2, r_distance = s._compute_exit_prices(
        fill_price=D("100"), entry_atr=2.0, is_long=False,
        bb_middle=98.0, bb_opposite=96.0,
        structure_stop_price=101.0,               # closer to entry than ATR stop 102
        tick_size=D("0.1"),
    )
    assert stop_price == D("101.0")
    assert r_distance == pytest.approx(1.0)


def test_compute_exit_prices_short_uses_atr_when_tighter():
    # ATR stop at 100 + 2 = 102; structure stop at 105 (looser)
    s = _build({"stop_atr_mult": 1.0})
    stop_price, _tp1, _tp2, r_distance = s._compute_exit_prices(
        fill_price=D("100"), entry_atr=2.0, is_long=False,
        bb_middle=98.0, bb_opposite=96.0,
        structure_stop_price=105.0,
        tick_size=D("0.1"),
    )
    assert stop_price == D("102.0")
    assert r_distance == pytest.approx(2.0)


def test_build_signal_returns_structure_stop_long():
    s = _build({"stop_atr_mult": 1.0, "structure_stop_buffer_atr_mult": 0.1,
                "pullback_proximity_pct": 100.0})
    # Need real buffer with candles for _build_signal to be called via real path;
    # here we call _build_signal directly with a crafted indicator.
    ind = _EntryIndicators(
        candles=[], close=100.0, prev_close=99.5, open_=99.0, low=99.5, high=100.5,
        prev_low=99.0, prev_high=100.0, volume=100.0,
        bb_upper=102.0, bb_middle=100.5, bb_lower=98.0,
        atr_now=1.0, atr_sma=1.0, rsi_now=25.0, vol_sma=200.0,
        swing_low=99.4, swing_high=101.0,   # swing_low tighter than ATR stop (99.0)
    )
    sig, atr_now, bb_mid, bb_opp, struct_stop = s._build_signal(
        "BTCUSDT", ind, Action.OPEN_LONG,
    )
    # structure_stop = swing_low - 0.1*ATR = 99.4 - 0.1 = 99.3
    assert struct_stop == pytest.approx(99.3)
    # final SL on the signal = max(ATR=99.0, struct=99.3) = 99.3
    assert float(sig.stop_loss_price) == pytest.approx(99.3)


def test_build_signal_returns_structure_stop_short():
    s = _build({"stop_atr_mult": 1.0, "structure_stop_buffer_atr_mult": 0.1})
    ind = _EntryIndicators(
        candles=[], close=100.0, prev_close=100.5, open_=100.7, low=99.5, high=101.0,
        prev_low=100.0, prev_high=101.5, volume=100.0,
        bb_upper=102.0, bb_middle=100.5, bb_lower=98.0,
        atr_now=1.0, atr_sma=1.0, rsi_now=75.0, vol_sma=200.0,
        swing_low=99.0, swing_high=100.6,   # swing_high tighter than ATR stop (101.0)
    )
    sig, _atr_now, _bb_mid, _bb_opp, struct_stop = s._build_signal(
        "BTCUSDT", ind, Action.OPEN_SHORT,
    )
    # structure_stop = swing_high + 0.1*ATR = 100.6 + 0.1 = 100.7
    assert struct_stop == pytest.approx(100.7)
    # final SL = min(ATR=101.0, struct=100.7) = 100.7
    assert float(sig.stop_loss_price) == pytest.approx(100.7)


# ---------------------------------------------------------------------------
# Break-even SL move (fires when position size shrinks vs initial)
# ---------------------------------------------------------------------------

def test_be_move_fires_when_position_size_shrinks():
    s = _build({"break_even_offset_atr_mult": 0.0})
    mp = _make_mp(side="LONG", entry=100.0, atr_val=2.0)
    mp.initial_qty = D("1.0")
    mp.stop_loss_order_id = 11

    # Live state: position open with 30% of initial qty (TP1 = 70% filled)
    live = _state(position=Position.LONG, size=D("0.3"))
    s.state_manager.get_state.return_value = live

    # Enough warmup so _entry_indicators returns something
    s._buffers["BTCUSDT"]["30m"] = [
        _c(i * MS_30M, 100, 101, 99, 100, 100) for i in range(60)
    ]
    s._managed["BTCUSDT"] = mp

    with patch("core.strategies.bb_rsi_mean_reversion.algo_orders.place_stop_market_order",
               return_value={"order_id": 999}) as place_stop, \
         patch("core.strategies.bb_rsi_mean_reversion.algo_orders.cancel_algo_order") as cancel_old, \
         patch.object(s, "_regime_adx_now", return_value=15.0):
        s._manage_position("BTCUSDT")
        place_stop.assert_called_once()
        args, _kw = place_stop.call_args
        # symbol, exit_side, qty, stop_price
        assert args[1] == "BTCUSDT"
        assert args[2] == "SELL"
        assert args[3] == D("0.3")       # qty matches remaining position
        assert args[4] == D("100.0")     # break-even = entry
        cancel_old.assert_called_once_with(s.client, "BTCUSDT", 11)
    assert mp.stop_moved_to_be is True
    assert mp.stop_loss_order_id == 999


def test_be_move_does_not_fire_again_after_first():
    s = _build()
    mp = _make_mp(side="LONG", entry=100.0, atr_val=2.0)
    mp.initial_qty = D("1.0")
    mp.stop_moved_to_be = True            # already moved once
    mp.stop_loss_order_id = 11

    s.state_manager.get_state.return_value = _state(position=Position.LONG, size=D("0.3"))
    s._buffers["BTCUSDT"]["30m"] = [
        _c(i * MS_30M, 100, 101, 99, 100, 100) for i in range(60)
    ]
    s._managed["BTCUSDT"] = mp

    with patch("core.strategies.bb_rsi_mean_reversion.algo_orders.place_stop_market_order") as place_stop, \
         patch.object(s, "_regime_adx_now", return_value=15.0):
        s._manage_position("BTCUSDT")
        place_stop.assert_not_called()


def test_be_move_does_not_fire_when_size_unchanged():
    s = _build()
    mp = _make_mp(side="LONG", entry=100.0, atr_val=2.0)
    mp.initial_qty = D("1.0")
    mp.stop_loss_order_id = 11

    # Full size still on position — no TP fill yet
    s.state_manager.get_state.return_value = _state(position=Position.LONG, size=D("1.0"))
    s._buffers["BTCUSDT"]["30m"] = [
        _c(i * MS_30M, 100, 101, 99, 100, 100) for i in range(60)
    ]
    s._managed["BTCUSDT"] = mp

    with patch("core.strategies.bb_rsi_mean_reversion.algo_orders.place_stop_market_order") as place_stop, \
         patch.object(s, "_regime_adx_now", return_value=15.0):
        s._manage_position("BTCUSDT")
        place_stop.assert_not_called()
    assert mp.stop_moved_to_be is False


def test_be_move_with_offset_above_entry_long():
    s = _build({"break_even_offset_atr_mult": 0.1})   # entry + 0.1*ATR
    mp = _make_mp(side="LONG", entry=100.0, atr_val=2.0)
    mp.initial_qty = D("1.0")
    mp.stop_loss_order_id = 11

    s.state_manager.get_state.return_value = _state(position=Position.LONG, size=D("0.3"))
    s._buffers["BTCUSDT"]["30m"] = [
        _c(i * MS_30M, 100, 101, 99, 100, 100) for i in range(60)
    ]
    s._managed["BTCUSDT"] = mp

    with patch("core.strategies.bb_rsi_mean_reversion.algo_orders.place_stop_market_order",
               return_value={"order_id": 999}) as place_stop, \
         patch("core.strategies.bb_rsi_mean_reversion.algo_orders.cancel_algo_order"), \
         patch.object(s, "_regime_adx_now", return_value=15.0):
        s._manage_position("BTCUSDT")
        args, _kw = place_stop.call_args
        # 100 + 0.1*2 = 100.2 (rounded to 0.1 tick)
        assert args[4] == D("100.2")


def test_be_move_short_places_buy_stop_at_entry():
    s = _build({"break_even_offset_atr_mult": 0.0})
    mp = _make_mp(side="SHORT", entry=100.0, atr_val=2.0)
    mp.initial_qty = D("1.0")
    mp.stop_loss_order_id = 11

    s.state_manager.get_state.return_value = _state(position=Position.SHORT, size=D("0.3"))
    s._buffers["BTCUSDT"]["30m"] = [
        _c(i * MS_30M, 100, 101, 99, 100, 100) for i in range(60)
    ]
    s._managed["BTCUSDT"] = mp

    with patch("core.strategies.bb_rsi_mean_reversion.algo_orders.place_stop_market_order",
               return_value={"order_id": 999}) as place_stop, \
         patch("core.strategies.bb_rsi_mean_reversion.algo_orders.cancel_algo_order"), \
         patch.object(s, "_regime_adx_now", return_value=15.0):
        s._manage_position("BTCUSDT")
        args, _kw = place_stop.call_args
        assert args[2] == "BUY"
        assert args[4] == D("100.0")


# ---------------------------------------------------------------------------
# Persistence — stop_moved_to_be round-trips through serialize/adopt
# ---------------------------------------------------------------------------

def test_serialize_includes_stop_moved_to_be():
    s = _build()
    mp = _make_mp(side="LONG", stop_moved_to_be=True)
    s._managed["BTCUSDT"] = mp
    blob = s.serialize_state("BTCUSDT")
    assert blob["stop_moved_to_be"] is True


def test_adopt_restores_stop_moved_to_be():
    s = _build()
    live_state = _state(position=Position.LONG, size=D("0.3"))
    live_state.orders = [
        {"order_id": 7, "side": "SELL", "is_algo": True},
    ]
    s.state_manager.get_state.return_value = live_state
    entry = {
        "strategy": "bb_rsi_mean_reversion", "side": "LONG",
        "entry_price": "100", "qty": "0.3",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 2.0,
                           "outside_band_streak": 0, "rsi_extreme_streak": 0,
                           "touched_middle": True, "stop_moved_to_be": True,
                           "entry_candle_open_time": 0},
        "orders": {"stop_loss_id": 7, "tp1_id": None, "tp2_id": None},
    }
    s.adopt("BTCUSDT", entry)
    assert s._managed["BTCUSDT"].stop_moved_to_be is True


def test_adopt_defaults_stop_moved_to_be_false_when_missing():
    s = _build()
    live_state = _state(position=Position.LONG, size=D("1"))
    live_state.orders = [
        {"order_id": 7, "side": "SELL", "is_algo": True},
    ]
    s.state_manager.get_state.return_value = live_state
    entry = {
        "strategy": "bb_rsi_mean_reversion", "side": "LONG",
        "entry_price": "100", "qty": "1",
        "strategy_state": {"entry_atr": 2.0, "r_distance": 2.0,
                           "outside_band_streak": 0, "rsi_extreme_streak": 0,
                           "touched_middle": False,
                           "entry_candle_open_time": 0},  # no stop_moved_to_be key
        "orders": {"stop_loss_id": 7, "tp1_id": None, "tp2_id": None},
    }
    s.adopt("BTCUSDT", entry)
    assert s._managed["BTCUSDT"].stop_moved_to_be is False


# ---------------------------------------------------------------------------
# Macro direction (_macro_direction)
# ---------------------------------------------------------------------------

def test_macro_direction_up_when_prices_rising():
    s = _build()
    # Steadily rising: close > EMA_slow AND EMA_fast > EMA_slow → UP
    rising = [_c(i * MS_1D, 100 + i, 101 + i, 99 + i, 100 + i, 100) for i in range(25)]
    s._buffers["BTCUSDT"]["1d"] = rising
    direction, msg = s._macro_direction("BTCUSDT")
    assert direction == "UP"
    assert "UP" in msg


def test_macro_direction_down_when_prices_falling():
    s = _build()
    # Steadily falling: close < EMA_slow AND EMA_fast < EMA_slow → DOWN
    falling = [_c(i * MS_1D, 125 - i, 126 - i, 124 - i, 125 - i, 100) for i in range(25)]
    s._buffers["BTCUSDT"]["1d"] = falling
    direction, msg = s._macro_direction("BTCUSDT")
    assert direction == "DOWN"
    assert "DOWN" in msg


def test_macro_direction_neutral_during_warmup():
    s = _build()
    # Far too few bars for ema_slow=10 warmup
    s._buffers["BTCUSDT"]["1d"] = [_c(i * MS_1D, 100, 101, 99, 100, 100) for i in range(5)]
    direction, msg = s._macro_direction("BTCUSDT")
    assert direction == "NEUTRAL"
    assert "warmup" in msg


def test_macro_direction_neutral_when_emafast_below_emaslow():
    # 15 bars @100, 5 bars @80, 1 bar @105.
    # After the spike+partial-recovery: close(105) > EMA10(≈90.5) but
    # EMA5(≈90.1) < EMA10 — neither clean UP nor clean DOWN → NEUTRAL.
    s = _build()
    bars = [_c(i * MS_1D, 100, 101, 99, 100, 100) for i in range(15)]
    bars += [_c((15 + i) * MS_1D, 80, 81, 79, 80, 100) for i in range(5)]
    bars += [_c(20 * MS_1D, 105, 106, 104, 105, 100)]
    s._buffers["BTCUSDT"]["1d"] = bars
    direction, msg = s._macro_direction("BTCUSDT")
    assert direction == "NEUTRAL"


# ---------------------------------------------------------------------------
# _compute_entry — macro gating (patches _regime_summary + _macro_direction)
# ---------------------------------------------------------------------------

def test_compute_entry_blocked_when_macro_neutral():
    s = _build()
    with patch.object(s, "_regime_summary", return_value=(True, "range")), \
         patch.object(s, "_macro_direction", return_value=("NEUTRAL", "warmup")):
        result = s._compute_entry("BTCUSDT")
    assert result is None


def test_compute_entry_up_macro_blocks_shorts():
    # UP macro: long gates fail → no short fallback → None
    s = _build()
    with patch.object(s, "_regime_summary", return_value=(True, "range")), \
         patch.object(s, "_macro_direction", return_value=("UP", "macro-UP")), \
         patch.object(s, "_entry_indicators", return_value=_ind()), \
         patch.object(s, "_first_failed_long_gate", return_value=("no_pierce", "detail")):
        result = s._compute_entry("BTCUSDT")
    assert result is None


def test_compute_entry_down_macro_blocks_longs():
    # DOWN macro: short gates fail → no long fallback → None
    s = _build()
    with patch.object(s, "_regime_summary", return_value=(True, "range")), \
         patch.object(s, "_macro_direction", return_value=("DOWN", "macro-DOWN")), \
         patch.object(s, "_entry_indicators", return_value=_ind()), \
         patch.object(s, "_first_failed_short_gate", return_value=("no_pierce", "detail")):
        result = s._compute_entry("BTCUSDT")
    assert result is None


def test_compute_entry_up_macro_allows_valid_long():
    s = _build()
    fake_result = ("signal", 1.0, 100.0, 105.0, 99.0)
    with patch.object(s, "_regime_summary", return_value=(True, "range")), \
         patch.object(s, "_macro_direction", return_value=("UP", "macro-UP")), \
         patch.object(s, "_entry_indicators", return_value=_ind()), \
         patch.object(s, "_first_failed_long_gate", return_value=None), \
         patch.object(s, "_build_signal", return_value=fake_result):
        result = s._compute_entry("BTCUSDT")
    assert result is fake_result


def test_compute_entry_down_macro_allows_valid_short():
    s = _build()
    fake_result = ("signal", 1.0, 100.0, 95.0, 101.0)
    with patch.object(s, "_regime_summary", return_value=(True, "range")), \
         patch.object(s, "_macro_direction", return_value=("DOWN", "macro-DOWN")), \
         patch.object(s, "_entry_indicators", return_value=_ind()), \
         patch.object(s, "_first_failed_short_gate", return_value=None), \
         patch.object(s, "_build_signal", return_value=fake_result):
        result = s._compute_entry("BTCUSDT")
    assert result is fake_result
