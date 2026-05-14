"""EMA trend + momentum strategy.

Multi-gate entry: EMA alignment + 1h trend filter + RVOL spike + RSI band + ADX regime.
Exit: opposite EMA cross or RSI exhaustion.

Signal pricing (placeholder until per-strategy pricing is reworked):
  entry_price       — current candle close
  stop_loss_price   — entry * (1 ± stop_loss_pct)
  take_profit_price — entry * (1 ± take_profit_pct)

Execution: IOC limit entry chasing the best ask/bid until filled or price drifts beyond
max_price_deviation_pct from signal price. After fill, places four reduceOnly exits:
stop-limit, stop-market, trailing TP, and GTC limit TP.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from loguru import logger

from core.strategies.base import Strategy
from core.types import Action, Position, Signal
from utils import algo_orders, market, orders
from utils.general import round_price
from utils.indicators import adx, ema, resample_to_1h, rsi


class EmaTrendMomentum(Strategy):
    """EMA trend + momentum strategy. See module docstring."""

    def candle_limit(self) -> int:
        interval_min = _interval_to_minutes(self.interval)
        return (200 * 60 // interval_min) + 50

    # ------------------------------------------------------------------
    # Signal
    # ------------------------------------------------------------------

    def compute_signal(self, symbol: str, candles: list[dict]) -> Optional[Signal]:
        p = self.params
        fast_period = int(p.get("fast_period", 9))
        slow_period = int(p.get("slow_period", 21))
        trend_period = int(p.get("trend_period", 200))
        rsi_period = int(p.get("rsi_period", 14))
        volume_lookback = int(p.get("volume_lookback", 20))
        volume_multiplier = Decimal(str(p.get("volume_multiplier", "1.2")))
        rsi_long_low = float(p.get("rsi_long_low", 50))
        rsi_long_high = float(p.get("rsi_long_high", 70))
        rsi_short_low = float(p.get("rsi_short_low", 30))
        rsi_short_high = float(p.get("rsi_short_high", 50))
        adx_period = int(p.get("adx_period", 14))
        min_adx = float(p.get("min_adx", 25))

        min_candles = max(slow_period + 1, rsi_period + 1, volume_lookback + 1, 2 * adx_period + 1)
        if len(candles) < min_candles:
            return None

        closes = [c["close"] for c in candles]

        candles_1h = resample_to_1h(candles)
        closes_1h = [c["close"] for c in candles_1h]
        if len(closes_1h) < trend_period:
            return None

        trend_ema = ema(closes_1h, trend_period)[-1]

        ema_slice = slow_period * 6 + 2
        fast_vals = ema(closes[-ema_slice:], fast_period)
        slow_vals = ema(closes[-ema_slice:], slow_period)
        if len(fast_vals) < 1 or len(slow_vals) < 1:
            return None

        fast_now = fast_vals[-1]
        slow_now = slow_vals[-1]
        rsi_slice = rsi_period * 10 + 1
        current_rsi = rsi(closes[-rsi_slice:], rsi_period)

        current_volume = candles[-1]["volume"]
        avg_volume = sum(c["volume"] for c in candles[-(volume_lookback + 1):-1]) / volume_lookback
        rvol = float(current_volume / avg_volume) if avg_volume > 0 else 0.0
        vol_spike = avg_volume > 0 and current_volume > avg_volume * volume_multiplier

        adx_slice = adx_period * 20 + 1
        adx_series = adx(candles[-adx_slice:], adx_period)
        current_adx: float = adx_series[-1] if adx_series else 0.0
        trending = current_adx >= min_adx

        current_price = float(closes[-1])
        above_trend = current_price > trend_ema
        below_trend = current_price < trend_ema
        ema_bullish = fast_now > slow_now
        ema_bearish = fast_now < slow_now

        gate_info = (
            f"EMA={'bull' if ema_bullish else 'bear' if ema_bearish else 'flat'} "
            f"above-1h-trend={above_trend} RVOL={rvol:.2f}x RSI={current_rsi:.1f} ADX={current_adx:.1f}"
        )
        logger.info("{} {} {} | price={:.4f} trend_ema={:.4f}",
                    self.tag, symbol, gate_info, current_price, trend_ema)

        entry_price = closes[-1]
        if ema_bullish and above_trend and vol_spike and rsi_long_low <= current_rsi <= rsi_long_high and trending:
            return self._build_open_signal(symbol, Action.OPEN_LONG, entry_price,
                                           reason=f"long gates passed: {gate_info}")
        if ema_bearish and below_trend and vol_spike and rsi_short_low <= current_rsi <= rsi_short_high and trending:
            return self._build_open_signal(symbol, Action.OPEN_SHORT, entry_price,
                                           reason=f"short gates passed: {gate_info}")

        return Signal(action=Action.HOLD, symbol=symbol, reason=f"no entry: {gate_info}")

    def _build_open_signal(self, symbol: str, action: Action, entry_price: Decimal, *, reason: str) -> Signal:
        sl_pct = Decimal(str(self.params.get("stop_loss_pct", 0.4))) / 100
        tp_pct = Decimal(str(self.params.get("take_profit_pct", 3.0))) / 100
        if action == Action.OPEN_LONG:
            sl = entry_price * (Decimal("1") - sl_pct)
            tp = entry_price * (Decimal("1") + tp_pct)
        else:
            sl = entry_price * (Decimal("1") + sl_pct)
            tp = entry_price * (Decimal("1") - tp_pct)
        return Signal(action=action, symbol=symbol, reason=reason,
                      entry_price=entry_price, stop_loss_price=sl, take_profit_price=tp)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute_open(self, signal: Signal) -> None:
        symbol = signal.symbol
        sym_info = self.sym_infos[symbol]
        tick_size = sym_info["tick_size"]
        step_size = sym_info["step_size"]

        is_long = signal.action == Action.OPEN_LONG
        entry_side = "BUY" if is_long else "SELL"
        exit_side = "SELL" if is_long else "BUY"

        max_usdt = Decimal(str(self.params.get("max_position_size_usdt", 100)))
        quantity = (max_usdt / signal.entry_price // step_size) * step_size
        if quantity <= 0:
            logger.warning("{} {} computed quantity is zero — skipping entry.", self.tag, symbol)
            return

        max_dev_pct = Decimal(str(self.params.get("max_price_deviation_pct", 0.3))) / 100

        self.state_manager.mark_change(symbol)
        filled = self._ioc_entry(symbol, entry_side, quantity, tick_size, signal.entry_price, max_dev_pct)
        if filled is None:
            return

        fill_qty = filled.get("executed_qty") or quantity
        if fill_qty <= 0:
            fill_qty = quantity
        fill_price = filled["price"] if filled["price"] > 0 else signal.entry_price

        self._place_protective_exits(
            symbol=symbol, side=exit_side, quantity=fill_qty,
            fill_price=fill_price, tick_size=tick_size,
            signal=signal, is_long=is_long,
        )
        self.state_manager.mark_change(symbol)

        if self.live_trade_manager is not None:
            self.live_trade_manager.register_open(symbol)

    # ------------------------------------------------------------------
    # IOC entry loop
    # ------------------------------------------------------------------

    def _ioc_entry(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        tick_size: Decimal,
        signal_price: Decimal,
        max_dev_pct: Decimal,
    ) -> Optional[dict]:
        filled_qty = Decimal("0")
        remaining = quantity
        attempt = 0
        while True:
            attempt += 1
            best_bid, best_ask = market.get_futures_best_bid_ask(self.client, symbol)
            aggressive = best_ask if side == "BUY" else best_bid
            dev = abs(aggressive - signal_price) / signal_price
            if dev > max_dev_pct:
                logger.warning("{} {} entry aborted: price drift {:.4f}% > max {:.4f}%",
                               self.tag, symbol, float(dev * 100), float(max_dev_pct * 100))
                if filled_qty > 0:
                    return {"price": signal_price, "executed_qty": filled_qty, "status": "PARTIALLY_FILLED"}
                return None

            limit_price = round_price(aggressive, tick_size)
            logger.info("{} {} IOC {} {} @ {} (attempt {})",
                        self.tag, symbol, side, remaining, limit_price, attempt)
            ioc = orders.place_limit_order(self.client, symbol, side, remaining, limit_price, time_in_force="IOC")

            time.sleep(0.1)
            try:
                status = orders.get_order(self.client, symbol, ioc["order_id"])
            except Exception as exc:
                logger.warning("{} {} could not query IOC {}: {}", self.tag, symbol, ioc["order_id"], exc)
                continue

            this_fill = status.get("executed_qty", Decimal("0"))
            if status["status"] in ("FILLED", "PARTIALLY_FILLED") and this_fill > 0:
                filled_qty += this_fill
                remaining = quantity - filled_qty
            if status["status"] == "FILLED" or remaining <= 0:
                logger.info("{} {} IOC fully filled after {} attempt(s)", self.tag, symbol, attempt)
                status["executed_qty"] = filled_qty
                return status

    # ------------------------------------------------------------------
    # Exit orders
    # ------------------------------------------------------------------

    def _place_protective_exits(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        fill_price: Decimal,
        tick_size: Decimal,
        signal: Signal,
        is_long: bool,
    ) -> None:
        """Place the four standard reduceOnly exits anchored to the actual fill price."""
        p = self.params
        sl_limit_pct = Decimal(str(p.get("stop_loss_pct", 0.4))) / 100
        sl_market_pct = Decimal(str(p.get("stop_loss_market_pct", 0.6))) / 100
        tp_pct = Decimal(str(p.get("take_profit_pct", 3.0))) / 100
        ttp_activation_pct = Decimal(str(p.get("trailing_take_profit_activation_pct", 1.0))) / 100
        ttp_callback_rate = Decimal(str(p.get("trailing_take_profit_callback_rate", 0.5)))

        if is_long:
            sl_limit = round_price(fill_price * (Decimal("1") - sl_limit_pct), tick_size)
            sl_market = round_price(fill_price * (Decimal("1") - sl_market_pct), tick_size)
            ttp_activation = round_price(fill_price * (Decimal("1") + ttp_activation_pct), tick_size)
            tp_limit = round_price(fill_price * (Decimal("1") + tp_pct), tick_size)
        else:
            sl_limit = round_price(fill_price * (Decimal("1") + sl_limit_pct), tick_size)
            sl_market = round_price(fill_price * (Decimal("1") + sl_market_pct), tick_size)
            ttp_activation = round_price(fill_price * (Decimal("1") - ttp_activation_pct), tick_size)
            tp_limit = round_price(fill_price * (Decimal("1") - tp_pct), tick_size)

        try:
            algo_orders.place_stop_limit_order(self.client, symbol, side, quantity, sl_limit, sl_limit)
        except Exception as exc:
            logger.error("{} {} stop-limit placement failed: {}", self.tag, symbol, exc)
        try:
            algo_orders.place_stop_market_order(self.client, symbol, side, quantity, sl_market)
        except Exception as exc:
            logger.error("{} {} stop-market placement failed: {}", self.tag, symbol, exc)
        try:
            orders.place_trailing_stop_order(self.client, symbol, side, quantity, ttp_callback_rate, ttp_activation)
        except Exception as exc:
            logger.error("{} {} trailing stop placement failed: {}", self.tag, symbol, exc)
        try:
            orders.place_tp_limit_order(self.client, symbol, side, quantity, tp_limit)
        except Exception as exc:
            logger.error("{} {} TP limit placement failed: {}", self.tag, symbol, exc)


def _interval_to_minutes(interval: str) -> int:
    n, unit = int(interval[:-1]), interval[-1]
    return {"m": n, "h": n * 60, "d": n * 1440}[unit]
