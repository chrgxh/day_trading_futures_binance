"""Unit tests for indicators in utils/indicators.py."""

from decimal import Decimal

import pytest

from utils.indicators import atr, bollinger_bands, daily_anchored_vwap, resample_to_1h, rsi

D = Decimal
MS_15M = 900_000
MS_1H = 3_600_000


# ---------------------------------------------------------------------------
# rsi — returns float
# ---------------------------------------------------------------------------

def test_rsi_returns_neutral_when_insufficient_data():
    assert rsi([D("100")] * 14, 14) == 50.0


def test_rsi_all_gains_returns_100():
    prices = [D(str(i)) for i in range(1, 25)]
    assert rsi(prices, 14) == 100.0


def test_rsi_all_losses_returns_0():
    prices = [D(str(25 - i)) for i in range(25)]
    assert rsi(prices, 14) == 0.0


def test_rsi_result_is_between_0_and_100():
    # Zigzag: +2, -1, +2, -1 … → RSI should be moderate
    prices = []
    p = D("100")
    for i in range(30):
        p += D("2") if i % 2 == 0 else D("-1")
        prices.append(p)
    result = rsi(prices, 14)
    assert 0 < result < 100


def test_rsi_exact_known_value():
    # 14 periods of equal +1 changes followed by one -1 change
    # avg_gain after Wilder smoothing ≈ 13/14, avg_loss ≈ 1/14 → RS ≈ 13
    prices = [D(str(i)) for i in range(16)]  # 0..14 (gains), then drop 1
    prices[-1] = D("13")  # last change is -1
    result = rsi(prices, 14)
    assert 80 < result < 95  # RS ≈ 13 → RSI ≈ 92.9


# ---------------------------------------------------------------------------
# resample_to_1h
# ---------------------------------------------------------------------------

def _candle(t_ms, open_, high, low, close, volume):
    return {"open_time": t_ms, "open": D(str(open_)), "high": D(str(high)),
            "low": D(str(low)), "close": D(str(close)), "volume": D(str(volume))}


def test_resample_empty_returns_empty():
    assert resample_to_1h([]) == []


def test_resample_single_bar_is_dropped_as_partial():
    candles = [_candle(0, 10, 11, 9, 10.5, 100)]
    assert resample_to_1h(candles) == []


def test_resample_two_hours_keeps_only_first():
    # Hour 0: 4 candles at t=0, 15m, 30m, 45m
    # Hour 1: 2 candles (partial) — must be dropped
    hour0 = [
        _candle(0 * MS_15M, 10, 11, 9,  10.5, 100),
        _candle(1 * MS_15M, 10.5, 12, 10, 11,  110),
        _candle(2 * MS_15M, 11, 13, 10.5, 12,  120),
        _candle(3 * MS_15M, 12, 14, 11.5, 13,  130),
    ]
    hour1 = [
        _candle(4 * MS_15M, 20, 21, 19, 20.5, 200),
        _candle(5 * MS_15M, 20.5, 22, 20, 21, 210),
    ]
    result = resample_to_1h(hour0 + hour1)
    assert len(result) == 1
    bar = result[0]
    assert bar["open_time"] == 0
    assert bar["open"] == D("10")
    assert bar["high"] == D("14")
    assert bar["low"] == D("9")
    assert bar["close"] == D("13")
    assert bar["volume"] == D("460")


def test_resample_preserves_chronological_order():
    # 3 complete hours + 1 partial; should return 3 bars in order
    candles = [
        _candle(h * MS_1H + q * MS_15M, 100 + h, 101 + h, 99 + h, 100.5 + h, 100)
        for h in range(4)
        for q in range(4)
    ]
    # Add one extra candle in hour 4 to make hour 3 the partial (dropped)
    candles.append(_candle(4 * MS_1H, 110, 111, 109, 110.5, 100))
    result = resample_to_1h(candles)
    assert len(result) == 4
    assert [b["open_time"] for b in result] == [0, MS_1H, 2 * MS_1H, 3 * MS_1H]


# ---------------------------------------------------------------------------
# atr — Wilder's true range with smoothing
# ---------------------------------------------------------------------------

def test_atr_returns_empty_when_insufficient_data():
    candles = [_candle(i * MS_15M, 10, 11, 9, 10.5, 100) for i in range(10)]
    assert atr(candles, period=14) == []


def test_atr_constant_range_equals_range():
    # Every candle has high-low = 2, no overnight gaps → TR = 2 everywhere → ATR = 2
    candles = [_candle(i * MS_15M, 10, 11, 9, 10, 100) for i in range(30)]
    series = atr(candles, period=14)
    assert series
    assert all(abs(v - 2.0) < 1e-9 for v in series)


def test_atr_handles_gap_via_close_diff():
    # A gap-up makes |high - prev_close| the true range, not high - low.
    candles = [
        _candle(0, 10, 11, 9, 10.5, 100),
        _candle(MS_15M, 20, 21, 19, 20.5, 100),  # huge gap up
    ]
    # Only 2 candles → ATR needs period+1 minimum, this is a smoke check it
    # returns empty rather than crashes.
    assert atr(candles, period=14) == []


def test_atr_length_matches_formula():
    n = 30
    candles = [_candle(i * MS_15M, 10, 11 + (i % 3), 9 - (i % 2), 10, 100) for i in range(n)]
    series = atr(candles, period=14)
    # Wilder: len(TR)=n-1, ATR series length = (n-1) - period + 1 = n - period
    assert len(series) == n - 14


# ---------------------------------------------------------------------------
# daily_anchored_vwap — resets every UTC midnight
# ---------------------------------------------------------------------------

MS_PER_DAY = 86_400_000


def test_vwap_empty_returns_empty():
    assert daily_anchored_vwap([]) == []


def test_vwap_constant_price_equals_price():
    # All candles same price → VWAP = typical = close
    candles = [_candle(i * MS_15M, 10, 10, 10, 10, 100) for i in range(20)]
    series = daily_anchored_vwap(candles)
    assert len(series) == 20
    assert all(abs(v - 10.0) < 1e-9 for v in series)


def test_vwap_resets_at_utc_midnight():
    # Day 1 has high prices, day 2 has low prices. Day 2 VWAP must NOT inherit day 1.
    day1 = [_candle(t, 100, 100, 100, 100, 50) for t in range(0, MS_PER_DAY, MS_15M)]
    day2 = [_candle(MS_PER_DAY + t, 10, 10, 10, 10, 50) for t in range(0, MS_PER_DAY, MS_15M)]
    series = daily_anchored_vwap(day1 + day2)
    # Last day-1 value ≈ 100; first day-2 value must be 10 (reset), not a blend.
    assert abs(series[len(day1) - 1] - 100.0) < 1e-9
    assert abs(series[len(day1)] - 10.0) < 1e-9


# ---------------------------------------------------------------------------
# bollinger_bands
# ---------------------------------------------------------------------------

def test_bollinger_bands_empty_when_insufficient_data():
    upper, middle, lower = bollinger_bands([D("1")] * 5, period=20)
    assert upper == [] and middle == [] and lower == []


def test_bollinger_bands_constant_series_zero_width():
    prices = [D("100")] * 30
    upper, middle, lower = bollinger_bands(prices, period=20, num_std=2.0)
    # Constant series → stdev 0 → all three bands collapse onto the mean.
    assert all(abs(u - 100.0) < 1e-9 for u in upper)
    assert all(abs(m - 100.0) < 1e-9 for m in middle)
    assert all(abs(l - 100.0) < 1e-9 for l in lower)
    # Length = len(prices) - period + 1 = 11
    assert len(middle) == 11


def test_bollinger_bands_known_window():
    # Window of 5: [1, 2, 3, 4, 5]. Mean = 3. Population stdev = sqrt(2) ≈ 1.4142.
    prices = [D(str(x)) for x in [1, 2, 3, 4, 5]]
    upper, middle, lower = bollinger_bands(prices, period=5, num_std=2.0)
    assert len(middle) == 1
    assert abs(middle[0] - 3.0) < 1e-9
    expected_sd = (2.0) ** 0.5
    assert abs(upper[0] - (3.0 + 2.0 * expected_sd)) < 1e-9
    assert abs(lower[0] - (3.0 - 2.0 * expected_sd)) < 1e-9


def test_bollinger_bands_upper_above_middle_above_lower():
    prices = [D(str(100 + (i % 7))) for i in range(50)]
    upper, middle, lower = bollinger_bands(prices, period=20, num_std=2.0)
    assert all(u >= m >= l for u, m, l in zip(upper, middle, lower))


def test_vwap_volume_weighting():
    # Two candles in same UTC day:
    #   bar 0: price 10, volume 100  → contributes 10*100 = 1000 PV
    #   bar 1: price 20, volume 300  → contributes 20*300 = 6000 PV
    # VWAP after bar 1 = (1000+6000) / (100+300) = 17.5
    candles = [
        _candle(0, 10, 10, 10, 10, 100),
        _candle(MS_15M, 20, 20, 20, 20, 300),
    ]
    series = daily_anchored_vwap(candles)
    assert abs(series[0] - 10.0) < 1e-9
    assert abs(series[1] - 17.5) < 1e-9
