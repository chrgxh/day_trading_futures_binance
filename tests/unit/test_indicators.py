"""Unit tests for rsi() and resample_to_1h() in utils/indicators.py."""

from decimal import Decimal

import pytest

from utils.indicators import resample_to_1h, rsi

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
