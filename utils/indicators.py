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


def sma(prices: list[Decimal], period: int) -> Decimal:
    """Simple moving average of the last `period` prices.

    Args:
        prices: Price series.
        period: Lookback window.
    """
    return sum(prices[-period:]) / period


def ema(prices: list[Decimal], period: int) -> list[Decimal]:
    """Exponential moving average seeded with the first SMA.

    Returns a list of length max(0, len(prices) - period + 1).
    Returns an empty list if there are fewer than `period` prices.

    Args:
        prices: Price series in chronological order.
        period: EMA period.
    """
    if len(prices) < period:
        return []
    k = Decimal("2") / Decimal(str(period + 1))
    result: list[Decimal] = [sum(prices[:period]) / period]
    for price in prices[period:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def macd(
    prices: list[Decimal],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[list[Decimal], list[Decimal], list[Decimal]]:
    """MACD — Moving Average Convergence/Divergence.

    Returns (macd_line, signal_line, histogram) aligned to the same length.
    All three lists are empty if there are insufficient prices.

    Args:
        prices: Closing prices in chronological order.
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


def adx(candles: list[dict], period: int = 14) -> list[Decimal]:
    """Average Directional Index using Wilder's smoothing.

    Returns the ADX series as a list of Decimal values.
    Returns an empty list if there are fewer than 2 * period + 1 candles.

    Args:
        candles: OHLCV dicts in chronological order; each must have 'high', 'low', 'close'.
        period: Wilder smoothing period (default 14).
    """
    if len(candles) < 2 * period + 1:
        return []

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    tr_vals: list[Decimal] = []
    plus_dm_vals: list[Decimal] = []
    minus_dm_vals: list[Decimal] = []

    for i in range(1, len(candles)):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        prev_h, prev_l = highs[i - 1], lows[i - 1]

        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        up_move = h - prev_h
        down_move = prev_l - l
        plus_dm = up_move if up_move > down_move and up_move > 0 else Decimal("0")
        minus_dm = down_move if down_move > up_move and down_move > 0 else Decimal("0")

        tr_vals.append(tr)
        plus_dm_vals.append(plus_dm)
        minus_dm_vals.append(minus_dm)

    # Wilder's initial smoothed values (sum of first `period` raw values)
    smooth_tr = sum(tr_vals[:period])
    smooth_plus = sum(plus_dm_vals[:period])
    smooth_minus = sum(minus_dm_vals[:period])

    dx_vals: list[Decimal] = []
    for i in range(period, len(tr_vals)):
        smooth_tr = smooth_tr - smooth_tr / period + tr_vals[i]
        smooth_plus = smooth_plus - smooth_plus / period + plus_dm_vals[i]
        smooth_minus = smooth_minus - smooth_minus / period + minus_dm_vals[i]

        if smooth_tr == 0:
            dx_vals.append(Decimal("0"))
            continue

        plus_di = Decimal("100") * smooth_plus / smooth_tr
        minus_di = Decimal("100") * smooth_minus / smooth_tr
        di_sum = plus_di + minus_di
        dx = Decimal("100") * abs(plus_di - minus_di) / di_sum if di_sum != 0 else Decimal("0")
        dx_vals.append(dx)

    if len(dx_vals) < period:
        return []

    # Wilder-smooth the DX series to produce ADX
    adx_val = sum(dx_vals[:period]) / period
    adx_series: list[Decimal] = [adx_val]
    for dx in dx_vals[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
        adx_series.append(adx_val)

    return adx_series
