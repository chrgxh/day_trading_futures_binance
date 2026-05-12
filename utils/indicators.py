"""Technical indicators and signal types."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

from loguru import logger


class Signal(Enum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


class Position(Enum):
    NONE = "NONE"
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class TradeSignal:
    """Output of a single strategy evaluation."""
    signal: Signal
    symbol: str
    reason: str
    suggested_quantity: Optional[Decimal] = None
    entry_price: Optional[Decimal] = None
    current_adx: Optional[float] = None   # strategy-computed; used by stagnation check in bot.py
    current_rsi: Optional[float] = None   # strategy-computed; used by stagnation check in bot.py
    suppress_reentry: bool = False         # set True on stagnation exits to block same-candle re-entry


def interval_to_minutes(interval: str) -> int:
    """Convert a Binance interval string to minutes (e.g. '5m' → 5, '1h' → 60)."""
    n, unit = int(interval[:-1]), interval[-1]
    return {"m": n, "h": n * 60, "d": n * 1440}[unit]


def sma(prices: list, period: int) -> float:
    """Simple moving average of the last `period` prices.

    Args:
        prices: Price series (Decimal or float).
        period: Lookback window.
    """
    return sum(float(p) for p in prices[-period:]) / period


def ema(prices: list, period: int) -> list[float]:
    """Exponential moving average seeded with the first SMA.

    Returns a list of length max(0, len(prices) - period + 1).
    Returns an empty list if there are fewer than `period` prices.
    Arithmetic is performed in float for performance.

    Args:
        prices: Price series in chronological order (Decimal or float).
        period: EMA period.
    """
    if len(prices) < period:
        return []
    data = [float(p) for p in prices]
    k = 2.0 / (period + 1)
    result: list[float] = [sum(data[:period]) / period]
    for price in data[period:]:
        result.append(price * k + result[-1] * (1.0 - k))
    return result


def macd(
    prices: list,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[list[float], list[float], list[float]]:
    """MACD — Moving Average Convergence/Divergence.

    Returns (macd_line, signal_line, histogram) aligned to the same length.
    All three lists are empty if there are insufficient prices.

    Args:
        prices: Closing prices in chronological order (Decimal or float).
        fast_period: Fast EMA period (default 12).
        slow_period: Slow EMA period (default 26).
        signal_period: Signal line EMA period (default 9).
    """
    fast_ema = ema(prices, fast_period)
    slow_ema = ema(prices, slow_period)

    if not fast_ema or not slow_ema:
        return [], [], []

    # Trim fast EMA head so it aligns index-for-index with slow EMA
    offset = slow_period - fast_period
    fast_aligned = fast_ema[offset:]
    macd_line = [f - s for f, s in zip(fast_aligned, slow_ema)]

    sig_line = ema(macd_line, signal_period)
    if not sig_line:
        return [], [], []

    macd_trimmed = macd_line[len(macd_line) - len(sig_line):]
    histogram = [m - s for m, s in zip(macd_trimmed, sig_line)]

    return macd_trimmed, sig_line, histogram


def rsi(prices: list, period: int = 14) -> float:
    """Relative Strength Index using Wilder's smoothing.

    Returns 50.0 (neutral) if there are fewer than period + 1 prices.
    Arithmetic is performed in float for performance.

    Args:
        prices: Closing prices in chronological order (Decimal or float).
        period: RSI period (default 14).
    """
    if len(prices) < period + 1:
        return 50.0

    data = [float(p) for p in prices]
    changes = [data[i] - data[i - 1] for i in range(1, len(data))]
    gains = [c if c > 0 else 0.0 for c in changes]
    losses = [-c if c < 0 else 0.0 for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def resample_to_1h(candles: list[dict]) -> list[dict]:
    """Aggregate sub-hourly OHLCV candles into complete 1h bars.

    Groups candles by their UTC hour and builds OHLCV for each hour.
    The last bar is dropped because it may be partially formed.

    Args:
        candles: OHLCV dicts with open_time in milliseconds.

    Returns:
        List of 1h OHLCV dicts sorted by open_time, excluding the latest (potentially partial) bar.
    """
    hourly: dict[int, dict] = {}
    ms_per_hour = 3_600_000
    for c in candles:
        hour_ts = (c["open_time"] // ms_per_hour) * ms_per_hour
        if hour_ts not in hourly:
            hourly[hour_ts] = {
                "open_time": hour_ts,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
            }
        else:
            bar = hourly[hour_ts]
            bar["high"] = max(bar["high"], c["high"])
            bar["low"] = min(bar["low"], c["low"])
            bar["close"] = c["close"]
            bar["volume"] += c["volume"]

    bars = sorted(hourly.values(), key=lambda x: x["open_time"])
    return bars[:-1] if bars else []


def adx(candles: list[dict], period: int = 14) -> list[float]:
    """Average Directional Index using Wilder's smoothing.

    Returns the ADX series as a list of float values.
    Returns an empty list if there are fewer than 2 * period + 1 candles.
    Arithmetic is performed in float for performance.

    Args:
        candles: OHLCV dicts in chronological order; each must have 'high', 'low', 'close'.
        period: Wilder smoothing period (default 14).
    """
    if len(candles) < 2 * period + 1:
        return []

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    tr_vals: list[float] = []
    plus_dm_vals: list[float] = []
    minus_dm_vals: list[float] = []

    for i in range(1, len(candles)):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        prev_h, prev_l = highs[i - 1], lows[i - 1]

        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        up_move = h - prev_h
        down_move = prev_l - l
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0

        tr_vals.append(tr)
        plus_dm_vals.append(plus_dm)
        minus_dm_vals.append(minus_dm)

    # Wilder's initial smoothed values (sum of first `period` raw values)
    smooth_tr = sum(tr_vals[:period])
    smooth_plus = sum(plus_dm_vals[:period])
    smooth_minus = sum(minus_dm_vals[:period])

    dx_vals: list[float] = []
    for i in range(period, len(tr_vals)):
        smooth_tr = smooth_tr - smooth_tr / period + tr_vals[i]
        smooth_plus = smooth_plus - smooth_plus / period + plus_dm_vals[i]
        smooth_minus = smooth_minus - smooth_minus / period + minus_dm_vals[i]

        if smooth_tr == 0.0:
            dx_vals.append(0.0)
            continue

        plus_di = 100.0 * smooth_plus / smooth_tr
        minus_di = 100.0 * smooth_minus / smooth_tr
        di_sum = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum != 0.0 else 0.0
        dx_vals.append(dx)

    if len(dx_vals) < period:
        return []

    # Wilder-smooth the DX series to produce ADX
    adx_val = sum(dx_vals[:period]) / period
    adx_series: list[float] = [adx_val]
    for dx in dx_vals[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
        adx_series.append(adx_val)

    return adx_series
