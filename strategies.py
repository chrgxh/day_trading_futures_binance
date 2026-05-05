"""
Pluggable trading strategies.

Each strategy function has the signature:
    (candles: list[dict], symbol: str, position: Position, params: dict) -> TradeSignal

The strategy is called every tick and returns the action the bot should take:
  OPEN_LONG  — open a long position (only when position is NONE)
  OPEN_SHORT — open a short position (only when position is NONE)
  CLOSE      — close the current position (only when position is LONG or SHORT)
  HOLD       — do nothing

Add a new strategy here and register it in STRATEGIES.
Switch strategies by setting 'strategy' in config.yaml.
"""

from typing import Callable

from loguru import logger

from utils.indicators import Position, Signal, TradeSignal, sma

StrategyFn = Callable[[list[dict], str, Position, dict], TradeSignal]


def ma_crossover(candles: list[dict], symbol: str, position: Position, params: dict) -> TradeSignal:
    """SMA crossover strategy.

    Entry: fast SMA crosses above slow SMA → long; crosses below → short.
    Exit:  the opposite crossover.

    Args:
        candles: OHLCV dicts from market.get_futures_ohlcv().
        symbol: Trading pair.
        position: Current open position state.
        params:
            fast_period  — fast SMA window (default 9)
            slow_period  — slow SMA window (default 21)
    """
    fast_period: int = params.get("fast_period", 9)
    slow_period: int = params.get("slow_period", 21)
    closes = [c["close"] for c in candles]

    if len(closes) < slow_period + 1:
        logger.warning("{} ma_crossover: need {} candles, have {}.", symbol, slow_period + 1, len(closes))
        return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="insufficient data")

    fast_now = sma(closes, fast_period)
    slow_now = sma(closes, slow_period)
    fast_prev = sma(closes[:-1], fast_period)
    slow_prev = sma(closes[:-1], slow_period)

    cross_up = fast_prev <= slow_prev and fast_now > slow_now
    cross_down = fast_prev >= slow_prev and fast_now < slow_now

    logger.debug("{} SMA({})={:.4f} SMA({})={:.4f}", symbol, fast_period, fast_now, slow_period, slow_now)

    if position == Position.LONG:
        if cross_down:
            return TradeSignal(signal=Signal.CLOSE, symbol=symbol, reason="fast SMA crossed below slow SMA")
        return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="holding long")

    if position == Position.SHORT:
        if cross_up:
            return TradeSignal(signal=Signal.CLOSE, symbol=symbol, reason="fast SMA crossed above slow SMA")
        return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="holding short")

    # No position — look for entry
    if cross_up:
        return TradeSignal(signal=Signal.OPEN_LONG, symbol=symbol, reason="fast SMA crossed above slow SMA")
    if cross_down:
        return TradeSignal(signal=Signal.OPEN_SHORT, symbol=symbol, reason="fast SMA crossed below slow SMA")
    return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="no crossover")


STRATEGIES: dict[str, StrategyFn] = {
    "ma_crossover": ma_crossover,
}
