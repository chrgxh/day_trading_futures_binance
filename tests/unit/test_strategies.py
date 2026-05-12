"""Unit tests for the ema_trend_momentum strategy."""

from decimal import Decimal

import pytest

from utils.indicators import Position, Signal
from strategies import ema_trend_momentum

D = Decimal
MS_15M = 900_000

# Reduced periods so tests only need ~60 candles instead of 840.
# trend_period=10 needs 10 complete 1h bars = 40 15m candles minimum.
_PARAMS = {
    "fast_period": 3,
    "slow_period": 5,
    "trend_period": 10,
    "rsi_period": 5,
    "volume_lookback": 5,
    "volume_multiplier": "1.2",
    "rsi_long_low": "0",       # wide open — isolate specific gates in dedicated tests
    "rsi_long_high": "100",
    "rsi_short_low": "0",
    "rsi_short_high": "100",
    "rsi_exit_overbought": "80",
    "rsi_exit_oversold": "20",
    "adx_period": 14,
    "min_adx": "0",            # disabled — isolate ADX gate in test_hold_when_adx_gate_fails
}


def _candles(n, start=100.0, step=0.1, volume=1000.0):
    """Generate n 15m OHLCV candles with a steady trend."""
    out = []
    for i in range(n):
        c = D(str(round(start + i * step, 6)))
        out.append({
            "open_time": i * MS_15M,
            "open": c - D("0.05"),
            "high": c + D("0.5"),
            "low": c - D("0.5"),
            "close": c,
            "volume": D(str(volume)),
        })
    return out


def _with_volume_spike(candles):
    """Return a copy with the last candle's volume doubled."""
    out = [dict(c) for c in candles]
    out[-1]["volume"] = out[-1]["volume"] * 2
    return out


# ---------------------------------------------------------------------------
# Insufficient-data guards
# ---------------------------------------------------------------------------

def test_hold_on_too_few_15m_candles():
    sig = ema_trend_momentum(_candles(4), "BTCUSDT", Position.NONE, _PARAMS)
    assert sig.signal == Signal.HOLD
    assert "insufficient" in sig.reason


def test_hold_when_not_enough_1h_bars():
    # 36 candles clears the min_candles guard (needs 29 with adx_period=14) but
    # produces only 8 complete 1h bars after resampling — below trend_period=10.
    sig = ema_trend_momentum(_candles(36), "BTCUSDT", Position.NONE, _PARAMS)
    assert sig.signal == Signal.HOLD
    assert "1h" in sig.reason


# ---------------------------------------------------------------------------
# Entry — no position
# ---------------------------------------------------------------------------

def test_long_entry_when_all_gates_pass():
    # Rising trend: fast EMA > slow EMA, price > trend EMA, RSI in range (wide bounds)
    candles = _with_volume_spike(_candles(60, step=0.1))
    sig = ema_trend_momentum(candles, "BTCUSDT", Position.NONE, _PARAMS)
    assert sig.signal == Signal.OPEN_LONG
    assert sig.entry_price is not None


def test_short_entry_when_all_gates_pass():
    # Falling trend: fast EMA < slow EMA, price < trend EMA
    candles = _with_volume_spike(_candles(60, start=110.0, step=-0.1))
    sig = ema_trend_momentum(candles, "BTCUSDT", Position.NONE, _PARAMS)
    assert sig.signal == Signal.OPEN_SHORT
    assert sig.entry_price is not None


def test_hold_when_volume_gate_fails():
    # All volumes identical → RVOL = 1.0 < 1.2 threshold → no entry
    candles = _candles(60, step=0.1, volume=1000.0)  # no spike
    sig = ema_trend_momentum(candles, "BTCUSDT", Position.NONE, _PARAMS)
    assert sig.signal == Signal.HOLD


def test_hold_when_rsi_above_long_band():
    params = dict(_PARAMS, rsi_long_low="50", rsi_long_high="70")
    # Steep uptrend → RSI near 100, above rsi_long_high → gate blocks entry
    candles = _with_volume_spike(_candles(60, step=1.0))
    sig = ema_trend_momentum(candles, "BTCUSDT", Position.NONE, params)
    assert sig.signal == Signal.HOLD


def test_hold_when_rsi_below_short_band():
    params = dict(_PARAMS, rsi_short_low="30", rsi_short_high="50")
    # Steep downtrend → RSI near 0, below rsi_short_low → gate blocks entry
    candles = _with_volume_spike(_candles(60, start=120.0, step=-1.0))
    sig = ema_trend_momentum(candles, "BTCUSDT", Position.NONE, params)
    assert sig.signal == Signal.HOLD


# ---------------------------------------------------------------------------
# Exit — in a position
# ---------------------------------------------------------------------------

def test_close_long_on_rsi_overbought():
    # Build flat candles then a sharp surge to push RSI(5) to 100
    flat = _candles(40, step=0.0)
    surge = [
        {
            "open_time": (40 + i) * MS_15M,
            "open": D(str(100 + i * 3)),
            "high": D(str(100 + (i + 1) * 3 + 0.5)),
            "low": D(str(100 + i * 3 - 0.5)),
            "close": D(str(100 + (i + 1) * 3)),
            "volume": D("1000"),
        }
        for i in range(8)
    ]
    sig = ema_trend_momentum(flat + surge, "BTCUSDT", Position.LONG, _PARAMS)
    assert sig.signal == Signal.CLOSE
    assert "overbought" in sig.reason


def test_close_short_on_rsi_oversold():
    # Build flat candles then a sharp plunge to push RSI(5) to 0
    flat = _candles(40, start=120.0, step=0.0)
    plunge = [
        {
            "open_time": (40 + i) * MS_15M,
            "open": D(str(120 - i * 3)),
            "high": D(str(120 - i * 3 + 0.5)),
            "low": D(str(120 - (i + 1) * 3 - 0.5)),
            "close": D(str(120 - (i + 1) * 3)),
            "volume": D("1000"),
        }
        for i in range(8)
    ]
    sig = ema_trend_momentum(flat + plunge, "BTCUSDT", Position.SHORT, _PARAMS)
    assert sig.signal == Signal.CLOSE
    assert "oversold" in sig.reason


def test_close_long_on_ema_cross_down():
    # Iterate tick-by-tick (as the real bot does) through a reversal and verify
    # the strategy emits a cross-down CLOSE at some point during the decline.
    # Start with 50 candles so the 1h gate (need 10 complete bars) is satisfied
    # throughout the entire reversal, not just at the end.
    rising = _candles(50, step=0.5)
    reversal = [
        {
            "open_time": (30 + i) * MS_15M,
            "open": D(str(115 - i * 2)),
            "high": D(str(115 - i * 2 + 0.3)),
            "low": D(str(115 - i * 2 - 2.3)),
            "close": D(str(115 - (i + 1) * 2)),
            "volume": D("1000"),
        }
        for i in range(15)
    ]
    buf = list(rising)
    for c in reversal:
        buf.append(c)
        sig = ema_trend_momentum(buf, "BTCUSDT", Position.LONG, _PARAMS)
        if sig.signal == Signal.CLOSE and "cross down" in sig.reason:
            return
    pytest.fail("Expected a cross-down CLOSE signal during the reversal")


def test_close_short_on_ema_cross_up():
    # Same tick-by-tick approach for a downtrend that reverses upward.
    # 50 starting candles to satisfy the 1h trend gate from the first reversal tick.
    falling = _candles(50, start=130.0, step=-0.5)
    recovery = [
        {
            "open_time": (30 + i) * MS_15M,
            "open": D(str(100 + i * 2)),
            "high": D(str(100 + i * 2 + 2.3)),
            "low": D(str(100 + i * 2 - 0.3)),
            "close": D(str(100 + (i + 1) * 2)),
            "volume": D("1000"),
        }
        for i in range(15)
    ]
    buf = list(falling)
    for c in recovery:
        buf.append(c)
        sig = ema_trend_momentum(buf, "BTCUSDT", Position.SHORT, _PARAMS)
        if sig.signal == Signal.CLOSE and "cross up" in sig.reason:
            return
    pytest.fail("Expected a cross-up CLOSE signal during the recovery")


def test_hold_long_on_no_exit_signal():
    # Rising prices guarantee no EMA cross-down. Setting rsi_exit_overbought=101
    # disables the RSI exit so this test purely verifies: in an established uptrend
    # with neither exit condition triggering, the strategy holds.
    params = dict(_PARAMS, rsi_exit_overbought="101")
    candles = _candles(60, step=0.1)
    sig = ema_trend_momentum(candles, "BTCUSDT", Position.LONG, params)
    assert sig.signal == Signal.HOLD
    assert "holding long" in sig.reason


# ---------------------------------------------------------------------------
# ADX regime gate
# ---------------------------------------------------------------------------

def test_hold_when_adx_gate_fails():
    # All other gates pass (trending candles + volume spike), but min_adx is set
    # impossibly high — verifies the ADX gate is wired into the entry condition.
    params = dict(_PARAMS, min_adx="200")
    candles = _with_volume_spike(_candles(60, step=0.1))
    sig = ema_trend_momentum(candles, "BTCUSDT", Position.NONE, params)
    assert sig.signal == Signal.HOLD


# ---------------------------------------------------------------------------
# TradeSignal indicator fields (bot.py reads current_adx/current_rsi from the signal
# instead of recomputing them — verify the contract holds for all post-computation paths)
# ---------------------------------------------------------------------------

def test_hold_signal_carries_indicator_values():
    # Any HOLD after indicators are computed should populate current_adx and current_rsi.
    candles = _candles(60, step=0.1)  # no volume spike → HOLD
    sig = ema_trend_momentum(candles, "BTCUSDT", Position.NONE, _PARAMS)
    assert sig.signal == Signal.HOLD
    assert sig.current_adx is not None
    assert sig.current_rsi is not None


def test_open_signal_carries_indicator_values():
    candles = _with_volume_spike(_candles(60, step=0.1))
    sig = ema_trend_momentum(candles, "BTCUSDT", Position.NONE, _PARAMS)
    assert sig.signal == Signal.OPEN_LONG
    assert sig.current_adx is not None
    assert sig.current_rsi is not None


def test_early_return_signals_have_no_indicator_values():
    # Early guards (insufficient data) fire before indicators are computed.
    sig = ema_trend_momentum(_candles(4), "BTCUSDT", Position.NONE, _PARAMS)
    assert sig.signal == Signal.HOLD
    assert sig.current_adx is None
    assert sig.current_rsi is None


def test_adx_gate_does_not_block_exits():
    # ADX only gates entry — an impossibly high min_adx must not suppress exit signals.
    params = dict(_PARAMS, min_adx="200")
    rising = _candles(50, step=0.5)
    reversal = [
        {
            "open_time": (50 + i) * MS_15M,
            "open": D(str(125 - i * 2)),
            "high": D(str(125 - i * 2 + 0.3)),
            "low": D(str(125 - i * 2 - 2.3)),
            "close": D(str(125 - (i + 1) * 2)),
            "volume": D("1000"),
        }
        for i in range(15)
    ]
    buf = list(rising)
    for c in reversal:
        buf.append(c)
        sig = ema_trend_momentum(buf, "BTCUSDT", Position.LONG, params)
        if sig.signal == Signal.CLOSE and "cross down" in sig.reason:
            return
    pytest.fail("ADX gate incorrectly suppressed a cross-down CLOSE signal")
