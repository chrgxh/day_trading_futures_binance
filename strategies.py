"""Trading strategies. Consume market data from utils.py; return trade signals."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

from loguru import logger


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    """Encapsulates a strategy's output for a single evaluation cycle."""
    signal: Signal
    symbol: str
    reason: str
    suggested_quantity: Optional[Decimal] = None


# ---------------------------------------------------------------------------
# Moving-average crossover strategy
# ---------------------------------------------------------------------------

def moving_average_crossover(
    klines: list[list],
    symbol: str,
    fast_period: int = 9,
    slow_period: int = 21,
) -> TradeSignal:
    """Simple moving-average crossover strategy.

    Emits BUY when the fast MA crosses above the slow MA, SELL when it
    crosses below, and HOLD otherwise.

    Args:
        klines: Raw kline data as returned by utils.get_klines().
        symbol: Trading pair these klines belong to.
        fast_period: Lookback window for the fast moving average.
        slow_period: Lookback window for the slow moving average.

    Returns:
        TradeSignal with BUY, SELL, or HOLD.
    """
    closes = [Decimal(k[4]) for k in klines]  # index 4 = close price

    if len(closes) < slow_period + 1:
        logger.warning(
            "Not enough candles for MA crossover on {} (have {}, need {}).",
            symbol, len(closes), slow_period + 1,
        )
        return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="insufficient data")

    def sma(prices: list[Decimal], period: int) -> Decimal:
        return sum(prices[-period:]) / period

    fast_now = sma(closes, fast_period)
    slow_now = sma(closes, slow_period)
    fast_prev = sma(closes[:-1], fast_period)
    slow_prev = sma(closes[:-1], slow_period)

    logger.debug(
        "{} MA({})={:.4f} MA({})={:.4f}",
        symbol, fast_period, fast_now, slow_period, slow_now,
    )

    if fast_prev <= slow_prev and fast_now > slow_now:
        logger.info("{} BUY signal: fast MA crossed above slow MA.", symbol)
        return TradeSignal(signal=Signal.BUY, symbol=symbol, reason="fast MA crossed above slow MA")

    if fast_prev >= slow_prev and fast_now < slow_now:
        logger.info("{} SELL signal: fast MA crossed below slow MA.", symbol)
        return TradeSignal(signal=Signal.SELL, symbol=symbol, reason="fast MA crossed below slow MA")

    return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="no crossover")
