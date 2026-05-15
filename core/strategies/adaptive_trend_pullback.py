"""Adaptive trend-pullback strategy (multi-timeframe).

Two fully configurable intervals — both come from `params` in config.yaml:
- `regime_interval` — read-only macro context, dictates long-or-short bias.
- `entry_interval` — all entry, exit, and trail decisions fire on close.

All indicator periods (EMA fast/slow, ADX, ATR, RSI, volume SMA, slope lookbacks,
invalidation thresholds, dead-trade gates) are configurable in `params`.

Entry (longs; shorts inverse):
  Regime: close > EMA_slow, EMA_fast > EMA_slow, EMA_fast slope positive over N bars.
  Entry:  pullback (one of the last K PRIOR bars actually touched the reference
          from the correct side — low <= EMA_fast/VWAP for longs, high >= for shorts),
          close > prev close, bullish close, volume > volume_SMA, ADX > min,
          ATR > ATR_SMA, RSI < max_long, close > EMA_fast, close > pullback_high.

Pullback semantics are DIRECTIONAL — a symmetric "within X% of EMA" check passes
nearly every bar in a trending market (the EMA lags so prices hover within a tick
of it), letting the breakout filter do all the work. The directional check requires
an actual retrace into the moving average before accepting a breakout.

SL/TP per signal:
  Stop  = close - stop_atr_mult * ATR.
  TP1   = close + tp1_r_multiple * R         (40% size, GTX post-only LIMIT reduce-only).
  R     = stop_atr_mult * ATR.

Exits managed inside this strategy on every CLOSED entry-interval candle:
  1. Trend invalidation (exit on ANY):
       - close below N-bar low / above N-bar high  (structure break)
       - EMA_fast slope flips                       (slope sign reverses)
       - close beyond EMA_fast by >= mult * ATR     (strong close against)
       - ADX drop > X over last K bars AND ADX < floor (momentum collapse)
  2. Dead-trade exit (after candles_since_entry > N, exit on ALL):
       - ADX < ADX K bars ago AND ADX < floor
       - ATR < SMA(ATR, M)
       - unrealized PnL per unit < r_floor * R
  3. Trailing stop:
       - For longs: new stop = max-close-since-entry - trail_atr_mult * ATR.
       - Only updated when more favorable than current. Place-then-cancel so the
         position is never momentarily unprotected.

Entry execution: IOC limit chasing the best ask/bid, re-quoting every
`ioc_poll_secs` until filled, drifted past `max_price_deviation_pct`, or
`entry_timeout_secs` elapsed. Default ioc_poll_secs is 3s to stay well clear
of Binance Futures rate limits.

This strategy does NOT use a LiveTradeManager — all post-fill lifecycle decisions
are tied to closed entry-interval candles, not StateManager poll cadence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger

from core.strategies.base import Strategy
from core.types import Action, Position, Signal
from utils import algo_orders, market, orders
from utils.general import PostOnlyRejected, round_price
from utils.indicators import adx, atr, daily_anchored_vwap, ema, rsi


@dataclass
class _ManagedPosition:
    """Per-position state tracked locally so candle-close logic can compute deltas."""

    symbol: str
    side: str                          # "LONG" or "SHORT"
    entry_price: Decimal               # actual avg fill price
    entry_atr: float                   # ATR at entry candle close — frozen for R math
    r_distance: float                  # stop_atr_mult * entry_atr (price-per-unit)
    initial_qty: Decimal
    entry_candle_open_time: int        # ms epoch of the candle that triggered entry
    highest_close: float               # running max close for longs
    lowest_close: float                # running min close for shorts
    current_stop_order_id: Optional[int] = None
    tp1_order_id: Optional[int] = None


@dataclass
class _EntryIndicators:
    """Bundle of indicator values needed for one entry decision — computed once."""

    candles: list[dict]
    close: float
    prev_close: float
    open_: float
    volume: float
    ema_now: float
    atr_now: float
    atr_sma: float
    adx_now: float
    rsi_now: float
    vol_sma: float
    vwap_now: Optional[float]
    pullback_bars: list[dict]
    pullback_high: float
    pullback_low: float


class AdaptiveTrendPullback(Strategy):
    """Regime-filtered pullback entry with adaptive ATR-driven exits.

    Intervals (regime + entry) and every indicator period are configurable in params.
    See module docstring.
    """

    _managed: dict[str, _ManagedPosition]  # narrows base's dict[str, Any]

    def __init__(self, *, params: dict, **kwargs) -> None:
        self._entry_interval = str(params.get("entry_interval", "30m"))
        self._regime_interval = str(params.get("regime_interval", "4h"))
        intervals = [self._entry_interval, self._regime_interval]
        super().__init__(intervals=intervals, params=params, **kwargs)

    # ------------------------------------------------------------------
    # Warmup sizing
    # ------------------------------------------------------------------

    def candle_limit(self, interval: str) -> int:
        p = self.params
        if interval == self._regime_interval:
            ema_slow = int(p.get("regime_ema_slow", 200))
            return ema_slow * 6 + 20
        ema_fast = int(p.get("ema_fast", 50))
        adx_period = int(p.get("adx_period", 14))
        atr_period = int(p.get("atr_period", 14))
        atr_sma_period = int(p.get("atr_sma_period", 20))
        invalidation_lookback = int(p.get("invalidation_structure_lookback", 20))
        dead_adx_lookback = int(p.get("dead_trade_adx_lookback", 6))
        return max(
            ema_fast * 6 + 20,
            adx_period * 20 + 5,
            atr_period + atr_sma_period + 5,
            invalidation_lookback + dead_adx_lookback + 5,
            250,
        )

    # ------------------------------------------------------------------
    # Tick routing (overrides base)
    # ------------------------------------------------------------------

    def _tick(self, symbol: str, interval: str) -> None:
        if interval == self._regime_interval and interval != self._entry_interval:
            return
        if interval != self._entry_interval:
            return

        self._sync_managed(symbol)

        if symbol in self._managed:
            self._manage_position(symbol)
            return

        if self.state_manager.has_position(symbol):
            logger.info("{} {} {} NO-ENTRY foreign-position — symbol already held by another strategy",
                        self.tag, symbol, self._entry_interval)
            return

        result = self._compute_entry(symbol)
        if result is None:
            return
        signal, entry_atr = result
        if not self.risk_guard.allow_open(symbol, self):
            logger.info("{} {} entry blocked by risk guard", self.tag, symbol)
            return
        self._execute_entry(signal, entry_atr)

    # ABC shims — the multi-interval flow uses helpers below; these keep the
    # class concrete and remain useful for ad-hoc callers / older tests.

    def compute_signal(self, symbol: str, candles: list[dict]) -> Optional[Signal]:
        result = self._compute_entry(symbol)
        return result[0] if result is not None else None

    def execute_open(self, signal: Signal) -> None:
        atr_val = self._latest_entry_atr(signal.symbol)
        if atr_val is None:
            logger.warning("{} {} execute_open without ATR — refusing.", self.tag, signal.symbol)
            return
        self._execute_entry(signal, atr_val)

    # ==================================================================
    # Entry decision (orchestrator + helpers)
    # ==================================================================

    def _compute_entry(self, symbol: str) -> Optional[tuple[Signal, float]]:
        long_ok, short_ok, regime_str = self._regime_summary(symbol)
        if not long_ok and not short_ok:
            logger.info("{} {} {} NO-ENTRY regime — {}",
                        self.tag, symbol, self._entry_interval, regime_str)
            return None

        ind = self._entry_indicators(symbol)
        if ind is None:
            logger.info("{} {} {} NO-ENTRY warmup — entry indicators not ready",
                        self.tag, symbol, self._entry_interval)
            return None

        long_fail: Optional[tuple[str, str]] = None
        short_fail: Optional[tuple[str, str]] = None

        if long_ok:
            long_fail = self._first_failed_long_gate(ind)
            if long_fail is None:
                return self._build_signal(symbol, ind, Action.OPEN_LONG)
        if short_ok:
            short_fail = self._first_failed_short_gate(ind)
            if short_fail is None:
                return self._build_signal(symbol, ind, Action.OPEN_SHORT)

        if long_ok and short_ok:
            logger.info("{} {} {} NO-ENTRY long-reject={}({}) short-reject={}({})",
                        self.tag, symbol, self._entry_interval,
                        long_fail[0], long_fail[1], short_fail[0], short_fail[1])
        elif long_ok:
            logger.info("{} {} {} NO-ENTRY long-reject — {} ({})",
                        self.tag, symbol, self._entry_interval,
                        long_fail[0], long_fail[1])
        else:
            logger.info("{} {} {} NO-ENTRY short-reject — {} ({})",
                        self.tag, symbol, self._entry_interval,
                        short_fail[0], short_fail[1])
        return None

    def _regime_bias(self, symbol: str) -> tuple[bool, bool]:
        """Return (regime_long_ok, regime_short_ok). Thin wrapper over _regime_summary."""
        long_ok, short_ok, _ = self._regime_summary(symbol)
        return long_ok, short_ok

    def _regime_summary(self, symbol: str) -> tuple[bool, bool, str]:
        """Return (long_ok, short_ok, diagnostic_str) from the regime-interval buffer.

        diagnostic_str is short and human-readable for use in HOLD logs — it shows
        whichever values were inspected (warmup state, close vs EMAs, slope).
        """
        p = self.params
        candles = self._buffers[symbol].get(self._regime_interval) or []
        ema_fast = int(p.get("regime_ema_fast", 50))
        ema_slow = int(p.get("regime_ema_slow", 200))
        slope_lookback = int(p.get("regime_slope_lookback", 5))

        needed = ema_slow + slope_lookback + 1
        if len(candles) < needed:
            return False, False, f"warmup ({len(candles)}/{needed} candles)"

        closes = [c["close"] for c in candles]
        fast_series = ema(closes, ema_fast)
        slow_series = ema(closes, ema_slow)
        if len(fast_series) <= slope_lookback or not slow_series:
            return False, False, "ema-warmup"

        close_now = float(closes[-1])
        ema_fast_now = fast_series[-1]
        ema_fast_then = fast_series[-1 - slope_lookback]
        ema_slow_now = slow_series[-1]
        slope = ema_fast_now - ema_fast_then

        long_ok = close_now > ema_slow_now and ema_fast_now > ema_slow_now and slope > 0
        short_ok = close_now < ema_slow_now and ema_fast_now < ema_slow_now and slope < 0
        summary = (f"close={close_now:.4f} ema_slow={ema_slow_now:.4f} "
                   f"ema_fast={ema_fast_now:.4f} slope={slope:+.4f}")
        return long_ok, short_ok, summary

    def _entry_indicators(self, symbol: str) -> Optional[_EntryIndicators]:
        """Compute every entry-interval indicator a single decision needs."""
        p = self.params
        candles = self._buffers[symbol][self._entry_interval]

        ema_fast = int(p.get("ema_fast", 50))
        adx_period = int(p.get("adx_period", 14))
        atr_period = int(p.get("atr_period", 14))
        atr_sma_period = int(p.get("atr_sma_period", 20))
        rsi_period = int(p.get("rsi_period", 14))
        vol_sma_period = int(p.get("volume_sma_period", 20))
        pullback_lookback = int(p.get("pullback_lookback", 3))
        slope_lookback = int(p.get("slope_lookback", 5))
        vwap_enabled = bool(p.get("vwap_enabled", True))

        min_needed = max(
            ema_fast + slope_lookback + 5,
            adx_period * 2 + 5,
            atr_period + atr_sma_period + 5,
            rsi_period + 2,
            vol_sma_period + 2,
            pullback_lookback + 2,
        )
        if len(candles) < min_needed:
            return None

        closes = [c["close"] for c in candles]
        ema_series = ema(closes, ema_fast)
        if len(ema_series) < slope_lookback + 1:
            return None
        atr_series = atr(candles, atr_period)
        if len(atr_series) < atr_sma_period:
            return None
        adx_series = adx(candles, adx_period)
        if not adx_series:
            return None
        vwap_series = daily_anchored_vwap(candles) if vwap_enabled else None

        last = candles[-1]
        prev = candles[-2]
        # Prior bars only — the breakout bar itself can't be its own pullback.
        pullback_bars = candles[-(pullback_lookback + 1):-1]

        return _EntryIndicators(
            candles=candles,
            close=float(last["close"]),
            prev_close=float(prev["close"]),
            open_=float(last["open"]),
            volume=float(last["volume"]),
            ema_now=ema_series[-1],
            atr_now=atr_series[-1],
            atr_sma=sum(atr_series[-atr_sma_period:]) / atr_sma_period,
            adx_now=adx_series[-1],
            rsi_now=rsi(closes, rsi_period),
            vol_sma=sum(float(c["volume"]) for c in candles[-(vol_sma_period + 1):-1]) / vol_sma_period,
            vwap_now=vwap_series[-1] if vwap_series else None,
            pullback_bars=pullback_bars,
            pullback_high=max(float(c["high"]) for c in pullback_bars),
            pullback_low=min(float(c["low"]) for c in pullback_bars),
        )

    def _pullback_ok(self, ind: _EntryIndicators, *, is_long: bool) -> bool:
        """Did at least one prior bar reach the reference FROM THE CORRECT SIDE?

        For longs: low must reach down to (or below) EMA_fast or VWAP, allowing a
        tiny tolerance ABOVE for noisy wicks. A bar whose low is comfortably above
        the reference is NOT a pullback.

        For shorts: symmetric — the bar's high must reach up to (or above) the
        reference.

        This is stricter than the older symmetric `|low - ema| / ema <= tol` check,
        which passes nearly every bar in a trending market and lets the breakout
        filter do all the work.
        """
        tolerance_frac = float(self.params.get("pullback_proximity_pct", 0.05)) / 100.0
        for c in ind.pullback_bars:
            if is_long:
                wick = float(c["low"])
                if ind.ema_now > 0 and wick <= ind.ema_now * (1.0 + tolerance_frac):
                    return True
                if ind.vwap_now is not None and ind.vwap_now > 0 \
                        and wick <= ind.vwap_now * (1.0 + tolerance_frac):
                    return True
            else:
                wick = float(c["high"])
                if ind.ema_now > 0 and wick >= ind.ema_now * (1.0 - tolerance_frac):
                    return True
                if ind.vwap_now is not None and ind.vwap_now > 0 \
                        and wick >= ind.vwap_now * (1.0 - tolerance_frac):
                    return True
        return False

    def _eval_long_entry(
        self, symbol: str, ind: _EntryIndicators,
    ) -> Optional[tuple[Signal, float]]:
        if self._first_failed_long_gate(ind) is not None:
            return None
        return self._build_signal(symbol, ind, Action.OPEN_LONG)

    def _eval_short_entry(
        self, symbol: str, ind: _EntryIndicators,
    ) -> Optional[tuple[Signal, float]]:
        if self._first_failed_short_gate(ind) is not None:
            return None
        return self._build_signal(symbol, ind, Action.OPEN_SHORT)

    def _first_failed_long_gate(
        self, ind: _EntryIndicators,
    ) -> Optional[tuple[str, str]]:
        """Walk long-entry gates in order; return (name, detail) of the first to fail.

        Returns None if every gate passes. Short-circuits — no work done after the
        first failure, so the reported reason is the earliest one in evaluation order.
        """
        p = self.params
        if not self._pullback_ok(ind, is_long=True):
            return ("pullback",
                    f"no_touch ema={ind.ema_now:.4f} vwap={ind.vwap_now}")
        if ind.close <= ind.open_:
            return ("bullish_close", f"close={ind.close:.4f} open={ind.open_:.4f}")
        if ind.close <= ind.prev_close:
            return ("higher_close", f"close={ind.close:.4f} prev={ind.prev_close:.4f}")
        if ind.vol_sma <= 0 or ind.volume <= ind.vol_sma:
            return ("volume", f"vol={ind.volume:.2f} sma={ind.vol_sma:.2f}")
        adx_min = float(p.get("adx_min", 20))
        if ind.adx_now <= adx_min:
            return ("adx", f"adx={ind.adx_now:.2f} min={adx_min}")
        if ind.atr_now <= ind.atr_sma:
            return ("atr_below_sma", f"atr={ind.atr_now:.4f} sma={ind.atr_sma:.4f}")
        rsi_max = float(p.get("rsi_max_long", 70))
        if ind.rsi_now >= rsi_max:
            return ("rsi_overbought", f"rsi={ind.rsi_now:.2f} max={rsi_max}")
        if ind.close <= ind.ema_now:
            return ("close_below_ema", f"close={ind.close:.4f} ema={ind.ema_now:.4f}")
        if ind.close <= ind.pullback_high:
            return ("pullback_breakout",
                    f"close={ind.close:.4f} pullback_high={ind.pullback_high:.4f}")
        return None

    def _first_failed_short_gate(
        self, ind: _EntryIndicators,
    ) -> Optional[tuple[str, str]]:
        p = self.params
        if not self._pullback_ok(ind, is_long=False):
            return ("pullback",
                    f"no_touch ema={ind.ema_now:.4f} vwap={ind.vwap_now}")
        if ind.close >= ind.open_:
            return ("bearish_close", f"close={ind.close:.4f} open={ind.open_:.4f}")
        if ind.close >= ind.prev_close:
            return ("lower_close", f"close={ind.close:.4f} prev={ind.prev_close:.4f}")
        if ind.vol_sma <= 0 or ind.volume <= ind.vol_sma:
            return ("volume", f"vol={ind.volume:.2f} sma={ind.vol_sma:.2f}")
        adx_min = float(p.get("adx_min", 20))
        if ind.adx_now <= adx_min:
            return ("adx", f"adx={ind.adx_now:.2f} min={adx_min}")
        if ind.atr_now <= ind.atr_sma:
            return ("atr_below_sma", f"atr={ind.atr_now:.4f} sma={ind.atr_sma:.4f}")
        rsi_min = float(p.get("rsi_min_short", 30))
        if ind.rsi_now <= rsi_min:
            return ("rsi_oversold", f"rsi={ind.rsi_now:.2f} min={rsi_min}")
        if ind.close >= ind.ema_now:
            return ("close_above_ema", f"close={ind.close:.4f} ema={ind.ema_now:.4f}")
        if ind.close >= ind.pullback_low:
            return ("pullback_breakdown",
                    f"close={ind.close:.4f} pullback_low={ind.pullback_low:.4f}")
        return None

    def _build_signal(
        self, symbol: str, ind: _EntryIndicators, action: Action,
    ) -> tuple[Signal, float]:
        p = self.params
        stop_atr_mult = float(p.get("stop_atr_mult", 1.5))
        tp1_r_multiple = float(p.get("tp1_r_multiple", 1.5))

        if action == Action.OPEN_LONG:
            sl = ind.close - stop_atr_mult * ind.atr_now
            tp = ind.close + tp1_r_multiple * (ind.close - sl)
            side_label = "LONG"
        else:
            sl = ind.close + stop_atr_mult * ind.atr_now
            tp = ind.close - tp1_r_multiple * (sl - ind.close)
            side_label = "SHORT"

        reason = self._gate_log(ind, side_label)
        logger.info("{} {} {} entry — {} | entry={:.4f} sl={:.4f} tp1={:.4f}",
                    self.tag, symbol, side_label, reason, ind.close, sl, tp)
        signal = Signal(
            action=action, symbol=symbol, reason=reason,
            entry_price=Decimal(str(ind.close)),
            stop_loss_price=Decimal(str(sl)),
            take_profit_price=Decimal(str(tp)),
        )
        return signal, ind.atr_now

    def _gate_log(self, ind: _EntryIndicators, side: str) -> str:
        return (
            f"side={side} ADX={ind.adx_now:.1f} ATR={ind.atr_now:.4f}/{ind.atr_sma:.4f} "
            f"RSI={ind.rsi_now:.1f} vol={ind.volume:.2f}/{ind.vol_sma:.2f}"
        )

    # ==================================================================
    # Entry execution (orchestrator + helpers)
    # ==================================================================

    def _execute_entry(self, signal: Signal, entry_atr: float) -> None:
        symbol = signal.symbol
        sym_info = self.sym_infos[symbol]
        is_long = signal.action == Action.OPEN_LONG

        qty = self._compute_position_qty(signal.entry_price, sym_info["step_size"])
        if qty <= 0:
            logger.warning("{} {} computed qty is zero (entry={}) — skipping",
                           self.tag, symbol, signal.entry_price)
            return

        self.state_manager.mark_change(symbol)
        fill = self._ioc_chase_for_signal(symbol, signal, qty, sym_info["tick_size"])
        if fill is None or fill["executed_qty"] <= 0:
            self.state_manager.mark_change(symbol)
            return

        filled_qty = fill["executed_qty"]
        fill_price = fill["price"] if fill["price"] > 0 else signal.entry_price

        stop_price, tp1_price, r_distance = self._compute_exit_prices(
            fill_price, entry_atr, is_long, sym_info["tick_size"],
        )

        exit_side = "SELL" if is_long else "BUY"
        tp1_order_id = self._maybe_place_tp1(
            symbol, exit_side, filled_qty, tp1_price, sym_info["step_size"],
        )
        stop_order_id = self._place_initial_stop(symbol, exit_side, filled_qty, stop_price)
        if stop_order_id is None:
            self._emergency_close(symbol)
            return

        self._record_managed(
            symbol=symbol, is_long=is_long,
            fill_price=fill_price, entry_atr=entry_atr, r_distance=r_distance,
            filled_qty=filled_qty, stop_order_id=stop_order_id, tp1_order_id=tp1_order_id,
        )
        self.state_manager.mark_change(symbol)
        logger.info("{} {} {} opened qty={} fill={} stop={} tp1={} (id={}, stop_id={})",
                    self.tag, symbol, "LONG" if is_long else "SHORT",
                    filled_qty, fill_price, stop_price, tp1_price, tp1_order_id, stop_order_id)

    def _compute_exit_prices(
        self, fill_price: Decimal, entry_atr: float, is_long: bool, tick_size: Decimal,
    ) -> tuple[Decimal, Decimal, float]:
        p = self.params
        stop_atr_mult = float(p.get("stop_atr_mult", 1.5))
        tp1_r_multiple = float(p.get("tp1_r_multiple", 1.5))
        r_distance = stop_atr_mult * entry_atr
        if is_long:
            stop_price = round_price(fill_price - Decimal(str(r_distance)), tick_size)
            tp1_price = round_price(fill_price + Decimal(str(tp1_r_multiple * r_distance)), tick_size)
        else:
            stop_price = round_price(fill_price + Decimal(str(r_distance)), tick_size)
            tp1_price = round_price(fill_price - Decimal(str(tp1_r_multiple * r_distance)), tick_size)
        return stop_price, tp1_price, r_distance

    def _maybe_place_tp1(
        self, symbol: str, exit_side: str, filled_qty: Decimal,
        tp1_price: Decimal, step_size: Decimal,
    ) -> Optional[int]:
        tp1_size_pct = float(self.params.get("tp1_size_pct", 0.40))
        tp1_qty = (filled_qty * Decimal(str(tp1_size_pct)) // step_size) * step_size
        if tp1_qty <= 0:
            return None
        return self._place_tp1_with_retries(symbol, exit_side, tp1_qty, tp1_price)

    def _record_managed(
        self, *, symbol: str, is_long: bool, fill_price: Decimal, entry_atr: float,
        r_distance: float, filled_qty: Decimal,
        stop_order_id: Optional[int], tp1_order_id: Optional[int],
    ) -> None:
        close = float(fill_price)
        self._managed[symbol] = _ManagedPosition(
            symbol=symbol,
            side="LONG" if is_long else "SHORT",
            entry_price=fill_price,
            entry_atr=entry_atr,
            r_distance=r_distance,
            initial_qty=filled_qty,
            entry_candle_open_time=self._buffers[symbol][self._entry_interval][-1]["open_time"],
            highest_close=close,
            lowest_close=close,
            current_stop_order_id=stop_order_id,
            tp1_order_id=tp1_order_id,
        )
        self.state_manager.register_owner(
            symbol,
            strategy_name=self.name,
            side="LONG" if is_long else "SHORT",
            entry_price=fill_price,
            qty=filled_qty,
            strategy_state=self.serialize_state(symbol),
            orders=self._orders_dict(symbol),
        )

    def _orders_dict(self, symbol: str) -> dict:
        mp = self._managed.get(symbol)
        if mp is None:
            return {}
        return {
            "stop_loss_id": mp.current_stop_order_id,
            "tp1_id": mp.tp1_order_id,
        }

    # ------------------------------------------------------------------
    # Persistence (overrides for restart recovery)
    # ------------------------------------------------------------------

    def serialize_state(self, symbol: str) -> dict:
        mp = self._managed.get(symbol)
        if mp is None:
            return {}
        return {
            "entry_atr": mp.entry_atr,
            "r_distance": mp.r_distance,
            "entry_candle_open_time": mp.entry_candle_open_time,
            "highest_close": mp.highest_close,
            "lowest_close": mp.lowest_close,
        }

    def adopt(self, symbol: str, entry: dict) -> None:
        """Rehydrate _ManagedPosition for `symbol` from the persisted entry.

        Reconciles saved order IDs against current open orders on Binance; if the
        saved stop is missing (cancelled or hit while the bot was down) and the
        position is still live, a fresh STOP_MARKET is placed at the original
        stop distance derived from entry_price and r_distance.
        """
        state = self.state_manager.get_state(symbol)
        if state.position == Position.NONE:
            logger.info("{} {} adopt skipped — Binance reports no position", self.tag, symbol)
            return

        side = entry.get("side") or state.position.value
        try:
            entry_price = Decimal(str(entry["entry_price"]))
            qty = Decimal(str(entry["qty"]))
        except (KeyError, ValueError) as exc:
            logger.error("{} {} adopt failed — bad entry/qty: {}", self.tag, symbol, exc)
            return

        ss = entry.get("strategy_state") or {}
        try:
            entry_atr = float(ss["entry_atr"])
            r_distance = float(ss["r_distance"])
        except (KeyError, ValueError, TypeError):
            logger.error("{} {} adopt failed — missing entry_atr/r_distance in saved state",
                         self.tag, symbol)
            return

        entry_candle_open_time = int(ss.get("entry_candle_open_time", 0))
        highest = float(ss.get("highest_close", float(entry_price)))
        lowest = float(ss.get("lowest_close", float(entry_price)))

        saved_orders = entry.get("orders") or {}
        live_ids = {o["order_id"] for o in state.orders}
        stop_id = saved_orders.get("stop_loss_id")
        tp1_id = saved_orders.get("tp1_id")
        stop_alive = stop_id in live_ids if stop_id is not None else False
        tp1_alive = tp1_id in live_ids if tp1_id is not None else False

        if not stop_alive:
            stop_id = self._adopt_replace_stop(symbol, side, qty, entry_price, r_distance)
        if not tp1_alive:
            tp1_id = None  # TP1 is optional; don't try to recreate it (sizing already happened)

        self._managed[symbol] = _ManagedPosition(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            entry_atr=entry_atr,
            r_distance=r_distance,
            initial_qty=qty,
            entry_candle_open_time=entry_candle_open_time,
            highest_close=highest,
            lowest_close=lowest,
            current_stop_order_id=stop_id,
            tp1_order_id=tp1_id,
        )
        # Push reconciled orders back to disk so the file mirrors live state.
        self.state_manager.update_owner(symbol, orders=self._orders_dict(symbol))

    def _ioc_chase_for_signal(
        self, symbol: str, signal: Signal, qty: Decimal, tick_size: Decimal,
    ) -> Optional[dict]:
        p = self.params
        side = "BUY" if signal.action == Action.OPEN_LONG else "SELL"
        max_dev_pct = Decimal(str(p.get("max_price_deviation_pct", 0.15))) / Decimal("100")
        ioc_poll = float(p.get("ioc_poll_secs", 3.0))
        timeout = float(p.get("entry_timeout_secs", 60.0))
        return self._ioc_chase(symbol, side, qty, tick_size, signal.entry_price,
                               max_dev_pct, ioc_poll, timeout)

    def _ioc_chase(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        tick_size: Decimal,
        signal_price: Decimal,
        max_dev_pct: Decimal,
        poll_secs: float,
        timeout_secs: float,
    ) -> Optional[dict]:
        """Loop best-price IOC limit orders until fully filled, drifted, or timed out."""
        filled_qty = Decimal("0")
        remaining = quantity
        deadline = time.time() + timeout_secs
        attempt = 0
        last_price = signal_price

        while remaining > 0 and time.time() < deadline:
            attempt += 1
            try:
                best_bid, best_ask = market.get_futures_best_bid_ask(self.client, symbol)
            except Exception as exc:
                logger.warning("{} {} could not read best bid/ask: {}", self.tag, symbol, exc)
                time.sleep(poll_secs)
                continue

            aggressive = best_ask if side == "BUY" else best_bid
            dev = abs(aggressive - signal_price) / signal_price
            if dev > max_dev_pct:
                logger.warning("{} {} entry aborted: price drift {:.4f}% > max {:.4f}%",
                               self.tag, symbol, float(dev * 100), float(max_dev_pct * 100))
                break

            limit_price = round_price(aggressive, tick_size)
            last_price = limit_price
            logger.info("{} {} IOC {} qty={} @ {} (attempt {})",
                        self.tag, symbol, side, remaining, limit_price, attempt)
            try:
                ioc = orders.place_limit_order(self.client, symbol, side, remaining,
                                               limit_price, time_in_force="IOC")
            except Exception as exc:
                logger.warning("{} {} IOC placement failed: {}", self.tag, symbol, exc)
                time.sleep(poll_secs)
                continue

            time.sleep(poll_secs)
            try:
                status = orders.get_order(self.client, symbol, ioc["order_id"])
            except Exception as exc:
                logger.warning("{} {} could not query IOC {}: {}",
                               self.tag, symbol, ioc["order_id"], exc)
                continue

            this_fill = status.get("executed_qty", Decimal("0"))
            if this_fill > 0:
                filled_qty += this_fill
                remaining = quantity - filled_qty
                last_price = status.get("price", last_price) or last_price

            if status["status"] == "FILLED" or remaining <= 0:
                logger.info("{} {} IOC fully filled after {} attempt(s)", self.tag, symbol, attempt)
                return {"price": last_price, "executed_qty": filled_qty, "status": "FILLED"}

        if filled_qty > 0:
            logger.warning("{} {} IOC partial fill qty={}/{} after timeout/drift",
                           self.tag, symbol, filled_qty, quantity)
            return {"price": last_price, "executed_qty": filled_qty, "status": "PARTIALLY_FILLED"}
        logger.warning("{} {} IOC unfilled after timeout/drift", self.tag, symbol)
        return None

    def _place_tp1_with_retries(
        self, symbol: str, side: str, qty: Decimal, price: Decimal,
    ) -> Optional[int]:
        retry_attempts = int(self.params.get("tp1_retry_attempts", 2))
        retry_interval = float(self.params.get("tp1_retry_interval_secs", 3.0))
        for attempt in range(retry_attempts + 1):
            try:
                order = orders.place_limit_order(self.client, symbol, side, qty, price,
                                                 time_in_force="GTX")
                return order["order_id"]
            except PostOnlyRejected:
                if attempt < retry_attempts:
                    logger.warning("{} {} TP1 GTX rejected — retrying in {}s ({}/{})",
                                   self.tag, symbol, retry_interval, attempt + 1, retry_attempts)
                    time.sleep(retry_interval)
                else:
                    logger.warning("{} {} TP1 GTX rejected {} times — position rides on trailing stop only",
                                   self.tag, symbol, retry_attempts + 1)
                    return None
            except (BinanceAPIException, BinanceRequestException) as exc:
                logger.error("{} {} TP1 placement error: {} — skipping TP1", self.tag, symbol, exc)
                return None
        return None

    # ==================================================================
    # Position management (orchestrator + helpers)
    # ==================================================================

    def _manage_position(self, symbol: str) -> None:
        mp = self._managed[symbol]
        candles = self._buffers[symbol][self._entry_interval]
        if not candles:
            return

        self._update_extrema(mp, candles)

        if self._check_trend_invalidation(mp, candles):
            self._exit_position(symbol, "trend invalidation")
            return

        candles_since_entry = sum(
            1 for c in candles if c["open_time"] > mp.entry_candle_open_time
        )
        if self._check_dead_trade(mp, candles, candles_since_entry):
            self._exit_position(symbol, "dead trade")
            return

        trail_moved = self._update_trailing_stop(mp, candles)
        if not trail_moved:
            self._log_managed_hold(mp, candles, candles_since_entry)

    def _log_managed_hold(
        self, mp: _ManagedPosition, candles: list[dict], candles_since_entry: int,
    ) -> None:
        """Emit a one-line HOLD log explaining why no exit/trail action was taken."""
        close = float(candles[-1]["close"])
        entry = float(mp.entry_price)
        unreal_per_unit = (close - entry) if mp.side == "LONG" else (entry - close)
        r_progress = unreal_per_unit / mp.r_distance if mp.r_distance > 0 else 0.0
        atr_period = int(self.params.get("atr_period", 14))
        atr_series = atr(candles, atr_period)
        atr_now = atr_series[-1] if atr_series else 0.0
        adx_series = adx(candles, int(self.params.get("adx_period", 14)))
        adx_now = adx_series[-1] if adx_series else 0.0
        extreme = mp.highest_close if mp.side == "LONG" else mp.lowest_close
        logger.info(
            "{} {} {} HOLD position — side={} close={:.4f} {}={:.4f} R={:+.2f} "
            "ADX={:.1f} ATR={:.4f} candles_since_entry={}",
            self.tag, mp.symbol, self._entry_interval, mp.side, close,
            "high_close" if mp.side == "LONG" else "low_close",
            extreme, r_progress, adx_now, atr_now, candles_since_entry,
        )

    def _update_extrema(self, mp: _ManagedPosition, candles: list[dict]) -> None:
        close = float(candles[-1]["close"])
        changed = False
        if mp.side == "LONG":
            if close > mp.highest_close:
                mp.highest_close = close
                changed = True
        else:
            if close < mp.lowest_close:
                mp.lowest_close = close
                changed = True
        if changed:
            self._persist_managed(mp.symbol)

    # ---- Trend invalidation: any-of -----------------------------------

    def _check_trend_invalidation(
        self, mp: _ManagedPosition, candles: list[dict],
    ) -> bool:
        for check in (
            self._inval_structure_break,
            self._inval_slope_flip,
            self._inval_strong_close_through_ema,
            self._inval_momentum_collapse,
        ):
            if check(mp, candles):
                return True
        return False

    def _inval_structure_break(
        self, mp: _ManagedPosition, candles: list[dict],
    ) -> bool:
        lookback = int(self.params.get("invalidation_structure_lookback", 20))
        if len(candles) <= lookback:
            return False
        close = float(candles[-1]["close"])
        prior = candles[-(lookback + 1):-1]
        if mp.side == "LONG":
            prior_low = min(float(c["low"]) for c in prior)
            if close < prior_low:
                logger.info("{} {} trend-inval: structure break (close {:.4f} < {}-bar low {:.4f})",
                            self.tag, mp.symbol, close, lookback, prior_low)
                return True
        else:
            prior_high = max(float(c["high"]) for c in prior)
            if close > prior_high:
                logger.info("{} {} trend-inval: structure break (close {:.4f} > {}-bar high {:.4f})",
                            self.tag, mp.symbol, close, lookback, prior_high)
                return True
        return False

    def _inval_slope_flip(
        self, mp: _ManagedPosition, candles: list[dict],
    ) -> bool:
        ema_fast = int(self.params.get("ema_fast", 50))
        slope_lookback = int(self.params.get("slope_lookback", 5))
        closes = [c["close"] for c in candles]
        ema_series = ema(closes, ema_fast)
        if len(ema_series) <= slope_lookback:
            return False
        slope = ema_series[-1] - ema_series[-1 - slope_lookback]
        if mp.side == "LONG" and slope <= 0:
            logger.info("{} {} trend-inval: slope flip (long, slope={:.6f})",
                        self.tag, mp.symbol, slope)
            return True
        if mp.side == "SHORT" and slope >= 0:
            logger.info("{} {} trend-inval: slope flip (short, slope={:.6f})",
                        self.tag, mp.symbol, slope)
            return True
        return False

    def _inval_strong_close_through_ema(
        self, mp: _ManagedPosition, candles: list[dict],
    ) -> bool:
        ema_fast = int(self.params.get("ema_fast", 50))
        atr_period = int(self.params.get("atr_period", 14))
        strong_mult = float(self.params.get("invalidation_strong_close_atr_mult", 0.5))
        closes = [c["close"] for c in candles]
        ema_series = ema(closes, ema_fast)
        atr_series = atr(candles, atr_period)
        if not ema_series or not atr_series:
            return False
        close = float(candles[-1]["close"])
        ema_now = ema_series[-1]
        atr_now = atr_series[-1]
        if mp.side == "LONG" and (ema_now - close) > strong_mult * atr_now:
            logger.info("{} {} trend-inval: strong close through EMA (long, gap={:.4f} > {:.4f}*ATR)",
                        self.tag, mp.symbol, ema_now - close, strong_mult)
            return True
        if mp.side == "SHORT" and (close - ema_now) > strong_mult * atr_now:
            logger.info("{} {} trend-inval: strong close through EMA (short, gap={:.4f} > {:.4f}*ATR)",
                        self.tag, mp.symbol, close - ema_now, strong_mult)
            return True
        return False

    def _inval_momentum_collapse(
        self, mp: _ManagedPosition, candles: list[dict],
    ) -> bool:
        adx_period = int(self.params.get("adx_period", 14))
        drop_lookback = int(self.params.get("invalidation_momentum_lookback", 3))
        adx_drop_threshold = float(self.params.get("invalidation_momentum_adx_drop", 5))
        adx_floor = float(self.params.get("invalidation_momentum_adx_floor", 20))
        adx_series = adx(candles, adx_period)
        if len(adx_series) <= drop_lookback:
            return False
        drop = adx_series[-1 - drop_lookback] - adx_series[-1]
        if drop > adx_drop_threshold and adx_series[-1] < adx_floor:
            logger.info("{} {} trend-inval: momentum collapse (ADX drop {:.2f} > {} AND ADX {:.2f} < {})",
                        self.tag, mp.symbol, drop, adx_drop_threshold, adx_series[-1], adx_floor)
            return True
        return False

    # ---- Dead-trade: all-of, after warmup -----------------------------

    def _check_dead_trade(
        self, mp: _ManagedPosition, candles: list[dict], candles_since_entry: int,
    ) -> bool:
        min_candles = int(self.params.get("dead_trade_min_candles", 24))
        if candles_since_entry <= min_candles:
            return False
        if not self._dead_adx_weakening(candles):
            return False
        if not self._dead_atr_compressing(candles):
            return False
        if not self._dead_pnl_below_r(mp, candles):
            return False
        logger.info("{} {} dead-trade exit: all gates true", self.tag, mp.symbol)
        return True

    def _dead_adx_weakening(self, candles: list[dict]) -> bool:
        adx_period = int(self.params.get("adx_period", 14))
        adx_lookback = int(self.params.get("dead_trade_adx_lookback", 6))
        adx_floor = float(self.params.get("dead_trade_adx_floor", 22))
        series = adx(candles, adx_period)
        if len(series) <= adx_lookback:
            return False
        return series[-1] < series[-1 - adx_lookback] and series[-1] < adx_floor

    def _dead_atr_compressing(self, candles: list[dict]) -> bool:
        atr_period = int(self.params.get("atr_period", 14))
        atr_sma_period = int(self.params.get("atr_sma_period", 20))
        series = atr(candles, atr_period)
        if len(series) < atr_sma_period:
            return False
        atr_sma_val = sum(series[-atr_sma_period:]) / atr_sma_period
        return series[-1] < atr_sma_val

    def _dead_pnl_below_r(self, mp: _ManagedPosition, candles: list[dict]) -> bool:
        last_close = float(candles[-1]["close"])
        entry = float(mp.entry_price)
        pnl_per_unit = (last_close - entry) if mp.side == "LONG" else (entry - last_close)
        r_floor = float(self.params.get("dead_trade_r_floor", 1.0))
        return pnl_per_unit < r_floor * mp.r_distance

    # ---- Trailing stop ------------------------------------------------

    def _update_trailing_stop(
        self, mp: _ManagedPosition, candles: list[dict],
    ) -> bool:
        """Return True if a new trailing stop was placed, False otherwise."""
        atr_period = int(self.params.get("atr_period", 14))
        atr_series = atr(candles, atr_period)
        if not atr_series:
            return False
        new_trail_price = self._compute_trail_level(mp, atr_series[-1])

        state = self.state_manager.get_state(mp.symbol)
        if state.position == Position.NONE or state.size <= 0:
            return False

        if not self._trail_is_more_favorable(mp, state, new_trail_price):
            return False

        self._replace_trail(mp, new_trail_price, state.size)
        return True

    def _compute_trail_level(self, mp: _ManagedPosition, atr_now: float) -> Decimal:
        trail_mult = float(self.params.get("trail_atr_mult", 2.0))
        tick_size = self.sym_infos[mp.symbol]["tick_size"]
        if mp.side == "LONG":
            raw = mp.highest_close - trail_mult * atr_now
        else:
            raw = mp.lowest_close + trail_mult * atr_now
        return round_price(Decimal(str(raw)), tick_size)

    def _trail_is_more_favorable(
        self, mp: _ManagedPosition, state, new_trail_price: Decimal,
    ) -> bool:
        if mp.current_stop_order_id is None:
            return True
        existing = next(
            (o for o in state.orders if o["order_id"] == mp.current_stop_order_id),
            None,
        )
        if existing is None:
            return True  # local id stale — let the replace happen, it'll resync
        if mp.side == "LONG":
            return new_trail_price > existing["stop_price"]
        return new_trail_price < existing["stop_price"]

    def _replace_trail(
        self, mp: _ManagedPosition, new_trail_price: Decimal, qty: Decimal,
    ) -> None:
        exit_side = "SELL" if mp.side == "LONG" else "BUY"
        self.state_manager.mark_change(mp.symbol)
        try:
            new_stop = algo_orders.place_stop_market_order(
                self.client, mp.symbol, exit_side, qty, new_trail_price,
            )
        except (BinanceAPIException, BinanceRequestException) as exc:
            logger.error("{} {} trailing stop placement failed: {} — keeping old stop",
                         self.tag, mp.symbol, exc)
            self.state_manager.mark_change(mp.symbol)
            return

        old_id = mp.current_stop_order_id
        mp.current_stop_order_id = new_stop["order_id"]
        if old_id is not None:
            try:
                algo_orders.cancel_algo_order(self.client, mp.symbol, old_id)
            except (BinanceAPIException, BinanceRequestException) as exc:
                logger.warning("{} {} could not cancel old stop {} after replacing: {}",
                               self.tag, mp.symbol, old_id, exc)
        self._persist_managed(mp.symbol)
        self.state_manager.mark_change(mp.symbol)
        logger.info("{} {} trail moved to {} (qty={}, new_id={})",
                    self.tag, mp.symbol, new_trail_price, qty, new_stop["order_id"])

