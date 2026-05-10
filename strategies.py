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

from decimal import Decimal
from typing import Callable

from loguru import logger

from utils.indicators import Position, Signal, TradeSignal, adx, ema, resample_to_1h, rsi, sma

StrategyFn = Callable[[list[dict], str, Position, dict], TradeSignal]


def ema_trend_momentum(candles: list[dict], symbol: str, position: Position, params: dict) -> TradeSignal:
    """Multi-gate EMA strategy: 1h trend filter + 15m crossover + RVOL + RSI momentum.

    Entry does not require a fresh crossover — any tick where all gates pass opens a trade.
    This handles cold-starts (bot starts with no position) and immediate re-entry after close.

    Gate summary:
      Long:  15m fast EMA > slow EMA, price above 1h 200 EMA, RVOL spike, RSI in [50, 70], ADX >= min_adx
      Short: 15m fast EMA < slow EMA, price below 1h 200 EMA, RVOL spike, RSI in [30, 50], ADX >= min_adx

    Exit:
      Long:  fast EMA crosses below slow EMA, or RSI >= rsi_exit_overbought (default 80)
      Short: fast EMA crosses above slow EMA, or RSI <= rsi_exit_oversold  (default 20)

    Requires ~840 15m candles in the buffer (config candle_limit) so that resampling
    produces 200+ complete 1h bars for the trend EMA.

    Args:
        candles: 15m OHLCV dicts from market.get_futures_ohlcv().
        symbol: Trading pair.
        position: Current open position state.
        params:
            fast_period         — fast EMA period (default 9)
            slow_period         — slow EMA period (default 21)
            trend_period        — EMA period on 1h for trend filter (default 200)
            rsi_period          — RSI period (default 14)
            volume_lookback     — candles for the RVOL baseline (default 20)
            volume_multiplier   — RVOL threshold multiplier (default 1.2)
            rsi_long_low        — RSI lower bound for long entry (default 50)
            rsi_long_high       — RSI upper bound for long entry (default 70)
            rsi_short_low       — RSI lower bound for short entry (default 30)
            rsi_short_high      — RSI upper bound for short entry (default 50)
            rsi_exit_overbought — RSI level to force-exit longs (default 80)
            rsi_exit_oversold   — RSI level to force-exit shorts (default 20)
            adx_period          — ADX smoothing period (default 14)
            min_adx             — minimum ADX for entry; below this the market is ranging (default 25)
    """
    fast_period: int = params.get("fast_period", 9)
    slow_period: int = params.get("slow_period", 21)
    trend_period: int = params.get("trend_period", 200)
    rsi_period: int = params.get("rsi_period", 14)
    volume_lookback: int = params.get("volume_lookback", 20)
    volume_multiplier = Decimal(str(params.get("volume_multiplier", "1.2")))
    rsi_long_low = Decimal(str(params.get("rsi_long_low", "50")))
    rsi_long_high = Decimal(str(params.get("rsi_long_high", "70")))
    rsi_short_low = Decimal(str(params.get("rsi_short_low", "30")))
    rsi_short_high = Decimal(str(params.get("rsi_short_high", "50")))
    rsi_exit_overbought = Decimal(str(params.get("rsi_exit_overbought", "80")))
    rsi_exit_oversold = Decimal(str(params.get("rsi_exit_oversold", "20")))
    adx_period: int = params.get("adx_period", 14)
    min_adx = Decimal(str(params.get("min_adx", "25")))

    min_15m = max(slow_period + 1, rsi_period + 1, volume_lookback + 1)
    if len(candles) < min_15m:
        logger.warning("{} ema_trend_momentum: need {} 15m candles, have {}.", symbol, min_15m, len(candles))
        return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="insufficient 15m data")

    closes = [c["close"] for c in candles]

    # --- Trend gate: resample 15m → 1h and compute the trend EMA ---
    candles_1h = resample_to_1h(candles)
    closes_1h = [c["close"] for c in candles_1h]

    if len(closes_1h) < trend_period:
        logger.warning(
            "{} ema_trend_momentum: need {} complete 1h bars, have {} (from {} 15m candles). "
            "Increase trading.candle_limit in config.yaml.",
            symbol, trend_period, len(closes_1h), len(candles),
        )
        return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="insufficient 1h data for trend EMA")

    trend_ema_vals = ema(closes_1h, trend_period)
    trend_ema = trend_ema_vals[-1]

    # --- 15m fast/slow EMA (need at least 2 values each for crossover detection) ---
    fast_vals = ema(closes, fast_period)
    slow_vals = ema(closes, slow_period)

    if len(fast_vals) < 2 or len(slow_vals) < 2:
        return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="insufficient data for 15m EMA")

    fast_now, fast_prev = fast_vals[-1], fast_vals[-2]
    slow_now, slow_prev = slow_vals[-1], slow_vals[-2]

    # --- RSI ---
    current_rsi = rsi(closes, rsi_period)

    # --- RVOL: compare this candle's volume to the rolling average of the previous N candles ---
    current_volume = candles[-1]["volume"]
    avg_volume = sum(c["volume"] for c in candles[-(volume_lookback + 1):-1]) / volume_lookback
    rvol = float(current_volume / avg_volume) if avg_volume > 0 else 0.0
    vol_spike = avg_volume > 0 and current_volume > avg_volume * volume_multiplier

    # --- ADX: regime filter — only enter in trending markets ---
    adx_series = adx(candles, adx_period)
    current_adx = adx_series[-1] if adx_series else Decimal("0")
    trending = current_adx >= min_adx

    # --- Derived state ---
    current_price = closes[-1]
    above_trend = current_price > trend_ema
    ema_bullish = fast_now > slow_now
    ema_bearish = fast_now < slow_now
    cross_up = fast_prev <= slow_prev and fast_now > slow_now
    cross_down = fast_prev >= slow_prev and fast_now < slow_now

    logger.info(
        "{} EMA{}={:.4f} EMA{}={:.4f} trend1h={:.4f} price={:.4f} RSI={:.2f} RVOL={:.2f}x ADX={:.1f}"
        " | ema={}{} trend={} vol={} rsi={} adx={}",
        symbol, fast_period, float(fast_now), slow_period, float(slow_now),
        float(trend_ema), float(current_price), float(current_rsi), rvol, float(current_adx),
        "bull" if ema_bullish else "bear" if ema_bearish else "flat",
        "(cross-up)" if cross_up else "(cross-down)" if cross_down else "",
        "above" if above_trend else "below",
        "SPIKE" if vol_spike else "low",
        ("long-zone" if rsi_long_low <= current_rsi <= rsi_long_high
         else "short-zone" if rsi_short_low <= current_rsi <= rsi_short_high
         else "neutral"),
        "trending" if trending else f"ranging(<{min_adx})",
    )

    # --- Exit logic ---
    if position == Position.LONG:
        if cross_down:
            return TradeSignal(signal=Signal.CLOSE, symbol=symbol,
                               reason=f"EMA cross down (RSI={current_rsi:.1f})")
        if current_rsi >= rsi_exit_overbought:
            return TradeSignal(signal=Signal.CLOSE, symbol=symbol,
                               reason=f"RSI overbought exit ({current_rsi:.1f} >= {rsi_exit_overbought})")
        return TradeSignal(signal=Signal.HOLD, symbol=symbol,
                           reason=f"holding long (RSI={current_rsi:.1f})")

    if position == Position.SHORT:
        if cross_up:
            return TradeSignal(signal=Signal.CLOSE, symbol=symbol,
                               reason=f"EMA cross up (RSI={current_rsi:.1f})")
        if current_rsi <= rsi_exit_oversold:
            return TradeSignal(signal=Signal.CLOSE, symbol=symbol,
                               reason=f"RSI oversold exit ({current_rsi:.1f} <= {rsi_exit_oversold})")
        return TradeSignal(signal=Signal.HOLD, symbol=symbol,
                           reason=f"holding short (RSI={current_rsi:.1f})")

    # --- Entry logic (position is NONE) ---
    # No fresh crossover required — EMAs already aligned is sufficient.
    # This covers: cold starts, immediate re-entries, and standard crossover entries.
    gate_info = (
        f"EMA={'bull' if ema_bullish else 'bear' if ema_bearish else 'flat'} "
        f"above-1h-trend={above_trend} RVOL={rvol:.2f}x RSI={current_rsi:.1f} ADX={current_adx:.1f}"
    )

    if ema_bullish and above_trend and vol_spike and rsi_long_low <= current_rsi <= rsi_long_high and trending:
        prefix = "cross-up + " if cross_up else ""
        return TradeSignal(
            signal=Signal.OPEN_LONG, symbol=symbol,
            reason=f"{prefix}long gates passed: {gate_info}",
            entry_price=current_price,
        )

    if ema_bearish and not above_trend and vol_spike and rsi_short_low <= current_rsi <= rsi_short_high and trending:
        prefix = "cross-down + " if cross_down else ""
        return TradeSignal(
            signal=Signal.OPEN_SHORT, symbol=symbol,
            reason=f"{prefix}short gates passed: {gate_info}",
            entry_price=current_price,
        )

    return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason=f"no entry: {gate_info}")


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

    if cross_up:
        return TradeSignal(signal=Signal.OPEN_LONG, symbol=symbol, reason="fast SMA crossed above slow SMA", entry_price=closes[-1])
    if cross_down:
        return TradeSignal(signal=Signal.OPEN_SHORT, symbol=symbol, reason="fast SMA crossed below slow SMA", entry_price=closes[-1])
    return TradeSignal(signal=Signal.HOLD, symbol=symbol, reason="no crossover")


STRATEGIES: dict[str, StrategyFn] = {
    "ema_trend_momentum": ema_trend_momentum,
    "ma_crossover": ma_crossover,
}
