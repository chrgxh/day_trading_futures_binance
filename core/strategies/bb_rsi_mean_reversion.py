"""Bollinger Bands + RSI mean-reversion strategy (multi-timeframe).

Three configurable intervals (all come from `params` in config.yaml):
- `macro_interval` — macro bias filter; determines which side is allowed.
- `regime_interval` — must be range-bound (ADX low, EMAs near each other) for any trade.
- `entry_interval` — all entry, exit, and management decisions fire on close.

Mean reversion takes the inverse view to a trend system: enter when price has
stretched too far from its moving average (a Bollinger Band pierce) and shown a
local rejection (close back inside the band with momentum oscillator extreme).
Hold for a snap back to the band middle; exit fast if the range thesis breaks.

Entry (longs; shorts inverse):
  Macro (macro_interval, e.g. 1d) — directional bias, one side only:
    - close > EMA_slow AND EMA_fast > EMA_slow → UP  (longs only; shorts blocked)
    - close < EMA_slow AND EMA_fast < EMA_slow → DOWN (shorts only; longs blocked)
    - Otherwise → NEUTRAL: skip all entries (avoids whipsaw in ambiguous macro)
  Regime (regime_interval):
    - ADX < regime_adx_max_range                  (range condition)
    - ADX <= regime_adx_min_trend                 (not in the trend zone)
    - |EMA_fast - EMA_slow| / close <= flatness   (EMAs not strongly separated)
  Entry (entry_interval):
    - Pierce: current_low OR prior bar(s) close/low below the lower band.
    - RSI < rsi_oversold.
    - close > bb_lower                            (reclaim into the band).
    - close > open                                (bullish candle).
    - close > prev_close                          (higher close).
    - volume <= volume_max_mult * volume_SMA      (no panic-spike on the bounce).
    - Avoid: pierce depth (bb_lower - close) > max_pierce_atr_mult * ATR.
    - Avoid: ATR > atr_max_expansion_mult * ATR_SMA   (volatility expanding).

SL/TP per signal — pick the MORE CONSERVATIVE (closer-to-entry) stop:
  ATR stop      = entry ∓ stop_atr_mult * ATR     (default 1.0; tighter than trend).
  Structure stop = swing_low/high over `structure_stop_lookback` bars,
                  ∓ structure_stop_buffer_atr_mult * ATR for headroom.
  Final stop    = LONG  → max(atr_stop, struct_stop)
                  SHORT → min(atr_stop, struct_stop)
  TP1   = bb_middle at signal close               (tp1_size_pct of qty, GTC reduce-only).
  TP2   = bb_opposite at signal close             (tp2_size_pct of qty, GTC reduce-only;
                                                   skipped if tp2_size_pct == 0).
  R     = entry − final_stop (signed by side).

Break-even SL move: once any TP partial-fills (position size shrinks vs initial),
the SL is replaced at entry (± break_even_offset_atr_mult * ATR) on the next
closed candle. Place-then-cancel ordering so the position is never unprotected.
The runner can no longer turn a partial winner back into a net loser.

Exits managed inside this strategy on every CLOSED entry-interval candle:
  1. Trend invalidation (exit on ANY):
       - 4h ADX > regime_adx_min_trend            (range thesis broken)
       - ATR > atr_max_expansion_mult * ATR_SMA   (volatility expansion)
       - N consecutive closes outside the relevant band   (max_outside_band_candles)
       - N consecutive RSI extremes against the trade     (max_rsi_extreme_candles)
       - close beyond entry by >= stop_atr_mult * entry_atr against the trade
         (hard SL re-check on close — the stop-market order handles intra-candle)
  2. Time exit:
       - candles_since_entry >= soft_max AND not touched middle AND close is on
         the wrong side of entry  ("trade simply isn't working")
       - candles_since_entry >= hard_max (unconditional)

Entry execution: a single IOC LIMIT at the signal close — fill at that price or
better, otherwise skip. This is intentionally NOT a chase loop. If the book has
already moved past the signal price by the time the order reaches Binance, the
IOC fills nothing and we walk away — the bounce already happened. Pays the
taker fee on entry by design; TPs are GTC LIMIT reduce-only (maker rebate).

This strategy does NOT use a LiveTradeManager — all post-fill lifecycle decisions
are tied to closed entry-interval candles, not StateManager poll cadence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger

from core.strategies.base import LayeredStopIds, Strategy
from core.types import Action, Position, Signal
from utils import orders
from utils.general import round_price
from utils.indicators import adx, atr, bollinger_bands, ema, rsi


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
    outside_band_streak: int = 0       # consecutive closes outside the relevant band
    rsi_extreme_streak: int = 0        # consecutive closes with RSI on the wrong side
    touched_middle: bool = False       # has the position ever reached the bb_middle?
    stop_moved_to_be: bool = False     # has the SL been moved to break-even after TP1?
    stop_ids: Optional[LayeredStopIds] = None   # layered stop (stop-limit + stop-market backstop)
    tp1_order_id: Optional[int] = None
    tp2_order_id: Optional[int] = None


@dataclass
class _EntryIndicators:
    """Bundle of indicator values needed for one entry decision — computed once."""

    candles: list[dict]
    close: float
    prev_close: float
    open_: float
    low: float
    high: float
    prev_low: float
    prev_high: float
    volume: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    atr_now: float
    atr_sma: float
    rsi_now: float
    vol_sma: float
    swing_low: float                   # lowest low over structure_stop_lookback bars
    swing_high: float                  # highest high over structure_stop_lookback bars


class BBRsiMeanReversion(Strategy):
    """Bollinger-Band / RSI mean-reversion strategy with hard time and regime exits.

    Intervals (macro + regime + entry) and every indicator period are configurable
    in params. See module docstring.
    """

    _managed: dict[str, _ManagedPosition]  # narrows base's dict[str, Any]

    def __init__(self, *, params: dict, **kwargs) -> None:
        self._entry_interval = str(params.get("entry_interval", "30m"))
        self._regime_interval = str(params.get("regime_interval", "4h"))
        self._macro_interval = str(params.get("macro_interval", "1d"))
        seen: set[str] = set()
        intervals: list[str] = []
        for iv in [self._entry_interval, self._regime_interval, self._macro_interval]:
            if iv not in seen:
                seen.add(iv)
                intervals.append(iv)
        super().__init__(intervals=intervals, params=params, **kwargs)

    # ------------------------------------------------------------------
    # Warmup sizing
    # ------------------------------------------------------------------

    def candle_limit(self, interval: str) -> int:
        """Return warmup bar count for `interval`, taking the max across all roles it fills.

        An interval may serve multiple roles (e.g. regime_interval == macro_interval).
        We accumulate needs from every matching role and return the maximum.
        """
        p = self.params
        needs: list[int] = [250]

        if interval == self._macro_interval:
            ema_slow = int(p.get("macro_ema_slow", 200))
            needs.append(ema_slow + 50)

        if interval == self._regime_interval:
            ema_slow = int(p.get("regime_ema_slow", 200))
            adx_period = int(p.get("regime_adx_period", 14))
            needs.extend([ema_slow * 6 + 20, adx_period * 20 + 5])

        if interval == self._entry_interval:
            bb_period = int(p.get("bb_period", 20))
            rsi_period = int(p.get("rsi_period", 14))
            atr_period = int(p.get("atr_period", 14))
            atr_sma_period = int(p.get("atr_sma_period", 20))
            vol_sma_period = int(p.get("volume_sma_period", 20))
            pierce_lookback = int(p.get("pierce_lookback", 2))
            structure_lookback = int(p.get("structure_stop_lookback", 5))
            needs.extend([
                bb_period * 5 + 10,
                rsi_period + 30,
                atr_period + atr_sma_period + 10,
                vol_sma_period + 5,
                pierce_lookback + 5,
                structure_lookback + 5,
            ])

        return max(needs)

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
        signal, entry_atr, bb_middle, bb_opposite, structure_stop = result
        if not self.risk_guard.allow_open(symbol, self):
            logger.info("{} {} entry blocked by risk guard", self.tag, symbol)
            return
        self._execute_entry(signal, entry_atr, bb_middle, bb_opposite, structure_stop)

    # ABC shims — kept concrete; the multi-interval flow uses the helpers below.

    def compute_signal(self, symbol: str, candles: list[dict]) -> Optional[Signal]:
        result = self._compute_entry(symbol)
        return result[0] if result is not None else None

    def execute_open(self, signal: Signal) -> None:
        atr_val = self._latest_entry_atr(signal.symbol)
        if atr_val is None:
            logger.warning("{} {} execute_open without ATR — refusing.", self.tag, signal.symbol)
            return
        ind = self._entry_indicators(signal.symbol)
        if ind is None:
            logger.warning("{} {} execute_open without entry indicators — refusing.", self.tag, signal.symbol)
            return
        is_long = signal.action == Action.OPEN_LONG
        opposite = ind.bb_upper if is_long else ind.bb_lower
        struct_buffer = float(self.params.get("structure_stop_buffer_atr_mult", 0.1))
        struct_stop = (ind.swing_low - struct_buffer * ind.atr_now if is_long
                       else ind.swing_high + struct_buffer * ind.atr_now)
        self._execute_entry(signal, atr_val, ind.bb_middle, opposite, struct_stop)

    # ==================================================================
    # Entry decision (orchestrator + helpers)
    # ==================================================================

    def _compute_entry(
        self, symbol: str,
    ) -> Optional[tuple[Signal, float, float, float, float]]:
        regime_ok, regime_str = self._regime_summary(symbol)
        if not regime_ok:
            logger.info("{} {} {} NO-ENTRY regime — {}",
                        self.tag, symbol, self._entry_interval, regime_str)
            return None

        macro_direction, macro_str = self._macro_direction(symbol)
        if macro_direction == "NEUTRAL":
            logger.info("{} {} {} NO-ENTRY macro — {}",
                        self.tag, symbol, self._entry_interval, macro_str)
            return None

        ind = self._entry_indicators(symbol)
        if ind is None:
            logger.info("{} {} {} NO-ENTRY warmup — entry indicators not ready",
                        self.tag, symbol, self._entry_interval)
            return None

        if macro_direction == "UP":
            long_fail = self._first_failed_long_gate(symbol, ind)
            if long_fail is None:
                return self._build_signal(symbol, ind, Action.OPEN_LONG)
            logger.info("{} {} {} NO-ENTRY macro-UP long-reject={}({})",
                        self.tag, symbol, self._entry_interval,
                        long_fail[0], long_fail[1])
            return None

        # macro_direction == "DOWN"
        short_fail = self._first_failed_short_gate(symbol, ind)
        if short_fail is None:
            return self._build_signal(symbol, ind, Action.OPEN_SHORT)
        logger.info("{} {} {} NO-ENTRY macro-DOWN short-reject={}({})",
                    self.tag, symbol, self._entry_interval,
                    short_fail[0], short_fail[1])
        return None

    def _regime_summary(self, symbol: str) -> tuple[bool, str]:
        """Return (range_ok, diagnostic_str) from the regime-interval buffer.

        Range conditions (both required):
          - ADX <= regime_adx_max_range            (clearly non-trending)
          - |EMA_fast - EMA_slow| / close <= flatness_pct
        ADX > regime_adx_min_trend explicitly disqualifies. The band between
        regime_adx_max_range and regime_adx_min_trend is the "gray zone" — no
        trade.
        """
        p = self.params
        candles = self._buffers[symbol].get(self._regime_interval) or []
        adx_period = int(p.get("regime_adx_period", 14))
        adx_max_range = float(p.get("regime_adx_max_range", 20.0))
        adx_min_trend = float(p.get("regime_adx_min_trend", 25.0))
        ema_fast_p = int(p.get("regime_ema_fast", 50))
        ema_slow_p = int(p.get("regime_ema_slow", 200))
        flatness_pct = float(p.get("regime_ema_flatness_pct", 2.0)) / 100.0

        needed = max(adx_period * 2 + 5, ema_slow_p + 5)
        if len(candles) < needed:
            return False, f"warmup ({len(candles)}/{needed} candles)"

        adx_series = adx(candles, adx_period)
        if not adx_series:
            return False, "adx-warmup"
        adx_now = adx_series[-1]

        if adx_now > adx_min_trend:
            return False, f"trending (ADX={adx_now:.2f} > {adx_min_trend})"
        if adx_now > adx_max_range:
            return False, f"gray-zone (ADX={adx_now:.2f} in ({adx_max_range}, {adx_min_trend}])"

        closes = [c["close"] for c in candles]
        fast = ema(closes, ema_fast_p)
        slow = ema(closes, ema_slow_p)
        if not fast or not slow:
            return False, "ema-warmup"
        close_now = float(closes[-1])
        if close_now <= 0:
            return False, "invalid-close"
        sep = abs(fast[-1] - slow[-1]) / close_now
        if sep > flatness_pct:
            return False, (f"trending-emas (sep={sep*100:.2f}% > "
                           f"{flatness_pct*100:.2f}%, ADX={adx_now:.2f})")

        return True, f"range (ADX={adx_now:.2f}, ema_sep={sep*100:.2f}%)"

    def _regime_adx_now(self, symbol: str) -> Optional[float]:
        """Just the current ADX on the regime interval. None if not warmed up."""
        candles = self._buffers[symbol].get(self._regime_interval) or []
        adx_period = int(self.params.get("regime_adx_period", 14))
        series = adx(candles, adx_period)
        return series[-1] if series else None

    def _macro_direction(self, symbol: str) -> tuple[str, str]:
        """Return ("UP"|"DOWN"|"NEUTRAL", diagnostic_str) from the macro-interval buffer.

        UP   : close > EMA_slow AND EMA_fast > EMA_slow → macro bullish, longs only.
        DOWN : close < EMA_slow AND EMA_fast < EMA_slow → macro bearish, shorts only.
        NEUTRAL: anything else → skip all entries (avoids whipsaw in ambiguous macro).
        """
        p = self.params
        candles = self._buffers[symbol].get(self._macro_interval) or []
        ema_fast_p = int(p.get("macro_ema_fast", 50))
        ema_slow_p = int(p.get("macro_ema_slow", 200))

        needed = ema_slow_p + 10
        if len(candles) < needed:
            return "NEUTRAL", f"warmup ({len(candles)}/{needed} candles)"

        closes = [c["close"] for c in candles]
        fast = ema(closes, ema_fast_p)
        slow = ema(closes, ema_slow_p)
        if not fast or not slow:
            return "NEUTRAL", "ema-warmup"

        close_now = float(closes[-1])
        ema_fast_now = fast[-1]
        ema_slow_now = slow[-1]
        diag = (f"close={close_now:.4f} EMA{ema_fast_p}={ema_fast_now:.4f} "
                f"EMA{ema_slow_p}={ema_slow_now:.4f}")

        if close_now > ema_slow_now and ema_fast_now > ema_slow_now:
            return "UP", f"macro-UP ({diag})"
        if close_now < ema_slow_now and ema_fast_now < ema_slow_now:
            return "DOWN", f"macro-DOWN ({diag})"
        return "NEUTRAL", f"macro-NEUTRAL ({diag})"

    def _entry_indicators(self, symbol: str) -> Optional[_EntryIndicators]:
        """Compute every entry-interval indicator a single decision needs."""
        p = self.params
        candles = self._buffers[symbol][self._entry_interval]

        bb_period = int(p.get("bb_period", 20))
        bb_num_std = float(p.get("bb_num_std", 2.0))
        atr_period = int(p.get("atr_period", 14))
        atr_sma_period = int(p.get("atr_sma_period", 20))
        rsi_period = int(p.get("rsi_period", 14))
        vol_sma_period = int(p.get("volume_sma_period", 20))
        structure_lookback = int(p.get("structure_stop_lookback", 5))

        min_needed = max(
            bb_period + 2,
            atr_period + atr_sma_period + 2,
            rsi_period + 2,
            vol_sma_period + 2,
            structure_lookback + 1,
        )
        if len(candles) < min_needed:
            return None

        closes = [c["close"] for c in candles]
        upper, middle, lower = bollinger_bands(closes, bb_period, bb_num_std)
        if not middle:
            return None
        atr_series = atr(candles, atr_period)
        if len(atr_series) < atr_sma_period:
            return None

        last = candles[-1]
        prev = candles[-2]
        recent = candles[-structure_lookback:] if structure_lookback > 0 else [last]
        swing_low = min(float(c["low"]) for c in recent)
        swing_high = max(float(c["high"]) for c in recent)
        return _EntryIndicators(
            candles=candles,
            close=float(last["close"]),
            prev_close=float(prev["close"]),
            open_=float(last["open"]),
            low=float(last["low"]),
            high=float(last["high"]),
            prev_low=float(prev["low"]),
            prev_high=float(prev["high"]),
            volume=float(last["volume"]),
            bb_upper=upper[-1],
            bb_middle=middle[-1],
            bb_lower=lower[-1],
            atr_now=atr_series[-1],
            atr_sma=sum(atr_series[-atr_sma_period:]) / atr_sma_period,
            rsi_now=rsi(closes, rsi_period),
            vol_sma=sum(float(c["volume"]) for c in candles[-(vol_sma_period + 1):-1]) / vol_sma_period,
            swing_low=swing_low,
            swing_high=swing_high,
        )

    def _pierced_lower(self, symbol: str, ind: _EntryIndicators) -> bool:
        """Did at least one of the last `pierce_lookback` bars (incl. current) reach below bb_lower?

        Long-side trigger: the bar's low went under the lower band OR its close
        closed under it. We check the current bar's low first (the cleanest
        reclaim setup: wick down + close back inside) before scanning prior bars.
        """
        lookback = int(self.params.get("pierce_lookback", 2))
        candles = self._buffers[symbol][self._entry_interval]
        scan = candles[-lookback:] if lookback > 0 else candles[-1:]
        for c in scan:
            if float(c["low"]) < ind.bb_lower or float(c["close"]) < ind.bb_lower:
                return True
        return False

    def _pierced_upper(self, symbol: str, ind: _EntryIndicators) -> bool:
        """Symmetric to _pierced_lower for shorts."""
        lookback = int(self.params.get("pierce_lookback", 2))
        candles = self._buffers[symbol][self._entry_interval]
        scan = candles[-lookback:] if lookback > 0 else candles[-1:]
        for c in scan:
            if float(c["high"]) > ind.bb_upper or float(c["close"]) > ind.bb_upper:
                return True
        return False

    def _first_failed_long_gate(
        self, symbol: str, ind: _EntryIndicators,
    ) -> Optional[tuple[str, str]]:
        """Walk long-entry gates in order; return (name, detail) of the first to fail."""
        p = self.params
        if not self._pierced_lower(symbol, ind):
            return ("no_pierce",
                    f"low={ind.low:.4f} prev_close={ind.prev_close:.4f} bb_lower={ind.bb_lower:.4f}")
        rsi_oversold = float(p.get("rsi_oversold", 30.0))
        if ind.rsi_now >= rsi_oversold:
            return ("rsi_not_oversold", f"rsi={ind.rsi_now:.2f} max={rsi_oversold}")
        if ind.close <= ind.bb_lower:
            return ("no_reclaim", f"close={ind.close:.4f} bb_lower={ind.bb_lower:.4f}")
        if ind.close <= ind.open_:
            return ("bearish_candle", f"close={ind.close:.4f} open={ind.open_:.4f}")
        if ind.close <= ind.prev_close:
            return ("lower_close", f"close={ind.close:.4f} prev={ind.prev_close:.4f}")
        vol_max_mult = float(p.get("volume_max_mult", 1.5))
        if ind.vol_sma > 0 and ind.volume > vol_max_mult * ind.vol_sma:
            return ("volume_spike",
                    f"vol={ind.volume:.2f} > {vol_max_mult}*sma={vol_max_mult * ind.vol_sma:.2f}")
        max_pierce_atr = float(p.get("max_pierce_atr_mult", 1.0))
        if ind.atr_now > 0 and (ind.bb_lower - ind.close) > max_pierce_atr * ind.atr_now:
            return ("pierce_too_deep",
                    f"gap={ind.bb_lower - ind.close:.4f} > {max_pierce_atr}*ATR={max_pierce_atr * ind.atr_now:.4f}")
        atr_max_mult = float(p.get("atr_max_expansion_mult", 1.2))
        if ind.atr_sma > 0 and ind.atr_now > atr_max_mult * ind.atr_sma:
            return ("atr_expanding",
                    f"atr={ind.atr_now:.4f} > {atr_max_mult}*sma={atr_max_mult * ind.atr_sma:.4f}")
        return None

    def _first_failed_short_gate(
        self, symbol: str, ind: _EntryIndicators,
    ) -> Optional[tuple[str, str]]:
        p = self.params
        if not self._pierced_upper(symbol, ind):
            return ("no_pierce",
                    f"high={ind.high:.4f} prev_close={ind.prev_close:.4f} bb_upper={ind.bb_upper:.4f}")
        rsi_overbought = float(p.get("rsi_overbought", 70.0))
        if ind.rsi_now <= rsi_overbought:
            return ("rsi_not_overbought", f"rsi={ind.rsi_now:.2f} min={rsi_overbought}")
        if ind.close >= ind.bb_upper:
            return ("no_reclaim", f"close={ind.close:.4f} bb_upper={ind.bb_upper:.4f}")
        if ind.close >= ind.open_:
            return ("bullish_candle", f"close={ind.close:.4f} open={ind.open_:.4f}")
        if ind.close >= ind.prev_close:
            return ("higher_close", f"close={ind.close:.4f} prev={ind.prev_close:.4f}")
        vol_max_mult = float(p.get("volume_max_mult", 1.5))
        if ind.vol_sma > 0 and ind.volume > vol_max_mult * ind.vol_sma:
            return ("volume_spike",
                    f"vol={ind.volume:.2f} > {vol_max_mult}*sma={vol_max_mult * ind.vol_sma:.2f}")
        max_pierce_atr = float(p.get("max_pierce_atr_mult", 1.0))
        if ind.atr_now > 0 and (ind.close - ind.bb_upper) > max_pierce_atr * ind.atr_now:
            return ("pierce_too_deep",
                    f"gap={ind.close - ind.bb_upper:.4f} > {max_pierce_atr}*ATR={max_pierce_atr * ind.atr_now:.4f}")
        atr_max_mult = float(p.get("atr_max_expansion_mult", 1.2))
        if ind.atr_sma > 0 and ind.atr_now > atr_max_mult * ind.atr_sma:
            return ("atr_expanding",
                    f"atr={ind.atr_now:.4f} > {atr_max_mult}*sma={atr_max_mult * ind.atr_sma:.4f}")
        return None

    def _build_signal(
        self, symbol: str, ind: _EntryIndicators, action: Action,
    ) -> tuple[Signal, float, float, float, float]:
        """Return (signal, entry_atr, bb_middle, bb_opposite, structure_stop_price).

        `structure_stop_price` is the swing-extreme stop with an ATR buffer, computed
        at signal time. `_compute_exit_prices` later picks the MORE CONSERVATIVE of
        the ATR stop (computed off fill_price) and this structure stop:
          - LONG  → max(atr_stop, structure_stop)  (the HIGHER level — closer to entry)
          - SHORT → min(atr_stop, structure_stop)  (the LOWER level — closer to entry)
        """
        p = self.params
        stop_atr_mult = float(p.get("stop_atr_mult", 1.0))
        struct_buffer = float(p.get("structure_stop_buffer_atr_mult", 0.1))
        is_long = action == Action.OPEN_LONG

        if is_long:
            atr_stop = ind.close - stop_atr_mult * ind.atr_now
            struct_stop = ind.swing_low - struct_buffer * ind.atr_now
            sl = max(atr_stop, struct_stop)
            tp = ind.bb_middle           # primary TP target = mean
            opposite = ind.bb_upper      # secondary TP target = far band
            side_label = "LONG"
        else:
            atr_stop = ind.close + stop_atr_mult * ind.atr_now
            struct_stop = ind.swing_high + struct_buffer * ind.atr_now
            sl = min(atr_stop, struct_stop)
            tp = ind.bb_middle
            opposite = ind.bb_lower
            side_label = "SHORT"

        reason = self._gate_log(ind, side_label)
        logger.info("{} {} {} entry — {} | entry={:.4f} sl={:.4f} "
                    "(atr_stop={:.4f}, struct_stop={:.4f}) tp1(mid)={:.4f} tp2(opp)={:.4f}",
                    self.tag, symbol, side_label, reason, ind.close, sl,
                    atr_stop, struct_stop, tp, opposite)
        signal = Signal(
            action=action, symbol=symbol, reason=reason,
            entry_price=Decimal(str(ind.close)),
            stop_loss_price=Decimal(str(sl)),
            take_profit_price=Decimal(str(tp)),
        )
        return signal, ind.atr_now, float(ind.bb_middle), float(opposite), float(struct_stop)

    def _gate_log(self, ind: _EntryIndicators, side: str) -> str:
        return (
            f"side={side} RSI={ind.rsi_now:.1f} ATR={ind.atr_now:.4f}/{ind.atr_sma:.4f} "
            f"bb=[{ind.bb_lower:.4f},{ind.bb_middle:.4f},{ind.bb_upper:.4f}] "
            f"vol={ind.volume:.2f}/{ind.vol_sma:.2f}"
        )

    # ==================================================================
    # Entry execution (orchestrator + helpers)
    # ==================================================================

    def _execute_entry(
        self, signal: Signal, entry_atr: float, bb_middle: float, bb_opposite: float,
        structure_stop_price: float,
    ) -> None:
        symbol = signal.symbol
        sym_info = self.sym_infos[symbol]
        is_long = signal.action == Action.OPEN_LONG
        tick_size = sym_info["tick_size"]
        side = "BUY" if is_long else "SELL"

        qty = self._compute_position_qty(signal.entry_price, sym_info["step_size"])
        if qty <= 0:
            logger.warning("{} {} computed qty is zero (entry={}) — skipping",
                           self.tag, symbol, signal.entry_price)
            return

        limit_price = round_price(signal.entry_price, tick_size)

        self.state_manager.mark_change(symbol)
        try:
            ioc = orders.place_limit_order(
                self.client, symbol, side, qty, limit_price, time_in_force="IOC",
            )
        except (BinanceAPIException, BinanceRequestException) as exc:
            logger.warning("{} {} IOC entry failed: {}", self.tag, symbol, exc)
            self.state_manager.mark_change(symbol)
            return

        filled_qty = ioc.get("executed_qty", Decimal("0"))
        if filled_qty <= 0:
            logger.info("{} {} IOC unfilled @ {} — signal price not reached, skipping",
                        self.tag, symbol, limit_price)
            self.state_manager.mark_change(symbol)
            return

        fill_price = ioc.get("price") or signal.entry_price
        if fill_price <= 0:
            fill_price = signal.entry_price

        stop_price, tp1_price, tp2_price, r_distance = self._compute_exit_prices(
            fill_price, entry_atr, is_long, bb_middle, bb_opposite,
            structure_stop_price, tick_size,
        )

        exit_side = "SELL" if is_long else "BUY"
        tp1_qty, tp2_qty = self._split_tp_qty(filled_qty, sym_info["step_size"])
        tp1_order_id = self._maybe_place_tp_limit(symbol, exit_side, tp1_qty, tp1_price, "TP1")
        tp2_order_id = self._maybe_place_tp_limit(symbol, exit_side, tp2_qty, tp2_price, "TP2") \
            if tp2_qty > 0 else None

        stop_ids = self._place_layered_stop(symbol, exit_side, filled_qty, stop_price)
        if stop_ids is None:
            self._emergency_close(symbol)
            return

        self._record_managed(
            symbol=symbol, is_long=is_long,
            fill_price=fill_price, entry_atr=entry_atr, r_distance=r_distance,
            filled_qty=filled_qty,
            stop_ids=stop_ids, tp1_order_id=tp1_order_id, tp2_order_id=tp2_order_id,
        )
        self.state_manager.mark_change(symbol)
        logger.info("{} {} {} opened qty={} fill={} stop={} tp1={} tp2={} "
                    "(stop_limit_id={}, stop_market_id={}, tp1_id={}, tp2_id={})",
                    self.tag, symbol, "LONG" if is_long else "SHORT",
                    filled_qty, fill_price, stop_price, tp1_price, tp2_price,
                    stop_ids.limit_id, stop_ids.market_id, tp1_order_id, tp2_order_id)

    def _compute_exit_prices(
        self,
        fill_price: Decimal,
        entry_atr: float,
        is_long: bool,
        bb_middle: float,
        bb_opposite: float,
        structure_stop_price: float,
        tick_size: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal, float]:
        """Pick the more conservative (closer-to-entry) of ATR stop and structure stop.

        TP1 = bb_middle, TP2 = bb_opposite (both at signal time).
        r_distance is recomputed against the actually-chosen stop so R-based math
        (dead trade, hard SL re-check, etc.) reflects the true risk taken.
        """
        stop_atr_mult = float(self.params.get("stop_atr_mult", 1.0))
        atr_offset = Decimal(str(stop_atr_mult * entry_atr))
        struct_stop_dec = Decimal(str(structure_stop_price))
        if is_long:
            atr_stop = fill_price - atr_offset
            chosen_stop = max(atr_stop, struct_stop_dec)
            r_distance = float(fill_price - chosen_stop)
        else:
            atr_stop = fill_price + atr_offset
            chosen_stop = min(atr_stop, struct_stop_dec)
            r_distance = float(chosen_stop - fill_price)
        stop_price = round_price(chosen_stop, tick_size)
        tp1_price = round_price(Decimal(str(bb_middle)), tick_size)
        tp2_price = round_price(Decimal(str(bb_opposite)), tick_size)
        return stop_price, tp1_price, tp2_price, r_distance

    def _split_tp_qty(
        self, filled_qty: Decimal, step_size: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Allocate filled_qty across TP1 and TP2 by their size percentages."""
        tp1_pct = Decimal(str(self.params.get("tp1_size_pct", 0.70)))
        tp2_pct = Decimal(str(self.params.get("tp2_size_pct", 0.30)))
        tp1_qty = (filled_qty * tp1_pct // step_size) * step_size
        if tp2_pct <= 0:
            return tp1_qty, Decimal("0")
        # TP2 takes whatever remains after TP1 (so we never exceed the filled qty
        # because of independent rounding).
        tp2_qty = ((filled_qty - tp1_qty) // step_size) * step_size
        return tp1_qty, tp2_qty

    def _maybe_place_tp_limit(
        self, symbol: str, exit_side: str, qty: Decimal, price: Decimal, label: str,
    ) -> Optional[int]:
        if qty <= 0:
            return None
        try:
            order = orders.place_tp_limit_order(self.client, symbol, exit_side, qty, price)
            return order["order_id"]
        except (BinanceAPIException, BinanceRequestException) as exc:
            logger.warning("{} {} {} placement error: {} — skipping {}",
                           self.tag, symbol, label, exc, label)
            return None

    def _record_managed(
        self, *,
        symbol: str, is_long: bool, fill_price: Decimal, entry_atr: float,
        r_distance: float, filled_qty: Decimal,
        stop_ids: LayeredStopIds, tp1_order_id: Optional[int], tp2_order_id: Optional[int],
    ) -> None:
        self._managed[symbol] = _ManagedPosition(
            symbol=symbol,
            side="LONG" if is_long else "SHORT",
            entry_price=fill_price,
            entry_atr=entry_atr,
            r_distance=r_distance,
            initial_qty=filled_qty,
            entry_candle_open_time=self._buffers[symbol][self._entry_interval][-1]["open_time"],
            stop_ids=stop_ids,
            tp1_order_id=tp1_order_id,
            tp2_order_id=tp2_order_id,
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
        stop_limit_id = mp.stop_ids.limit_id if mp.stop_ids is not None else None
        stop_market_id = mp.stop_ids.market_id if mp.stop_ids is not None else None
        return {
            "stop_limit_id": stop_limit_id,
            "stop_market_id": stop_market_id,
            "tp1_id": mp.tp1_order_id,
            "tp2_id": mp.tp2_order_id,
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
            "outside_band_streak": mp.outside_band_streak,
            "rsi_extreme_streak": mp.rsi_extreme_streak,
            "touched_middle": mp.touched_middle,
            "stop_moved_to_be": mp.stop_moved_to_be,
        }

    def adopt(self, symbol: str, entry: dict) -> None:
        """Rehydrate _ManagedPosition for `symbol` from the persisted entry.

        Reconciles saved order IDs against current open orders on Binance; if the
        saved stop is missing (cancelled or hit while the bot was down) and the
        position is still live, a fresh STOP_MARKET is placed at the original
        stop distance derived from entry_price and r_distance. Missing TPs are
        not recreated (their qty was already split off the original fill).
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
        outside_streak = int(ss.get("outside_band_streak", 0))
        rsi_streak = int(ss.get("rsi_extreme_streak", 0))
        touched_middle = bool(ss.get("touched_middle", False))
        stop_moved_to_be = bool(ss.get("stop_moved_to_be", False))

        saved_orders = entry.get("orders") or {}
        live_ids = {o["order_id"] for o in state.orders}
        saved_limit_id = saved_orders.get("stop_limit_id")
        saved_market_id = saved_orders.get("stop_market_id")
        tp1_id = saved_orders.get("tp1_id")
        tp2_id = saved_orders.get("tp2_id")

        limit_alive = saved_limit_id in live_ids if saved_limit_id is not None else False
        market_alive = saved_market_id in live_ids if saved_market_id is not None else False
        tp1_alive = tp1_id in live_ids if tp1_id is not None else False
        tp2_alive = tp2_id in live_ids if tp2_id is not None else False

        if limit_alive and market_alive:
            stop_ids: Optional[LayeredStopIds] = LayeredStopIds(
                limit_id=saved_limit_id, market_id=saved_market_id,
            )
        else:
            survivor = LayeredStopIds(
                limit_id=saved_limit_id if limit_alive else None,
                market_id=saved_market_id if market_alive else None,
            )
            stop_ids = self._adopt_replace_layered_stop(
                symbol, side, qty, entry_price, r_distance, survivor_ids=survivor,
            )
        if not tp1_alive:
            tp1_id = None
        if not tp2_alive:
            tp2_id = None

        self._managed[symbol] = _ManagedPosition(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            entry_atr=entry_atr,
            r_distance=r_distance,
            initial_qty=qty,
            entry_candle_open_time=entry_candle_open_time,
            outside_band_streak=outside_streak,
            rsi_extreme_streak=rsi_streak,
            touched_middle=touched_middle,
            stop_moved_to_be=stop_moved_to_be,
            stop_ids=stop_ids,
            tp1_order_id=tp1_id,
            tp2_order_id=tp2_id,
        )
        self.state_manager.update_owner(symbol, orders=self._orders_dict(symbol))

    # ==================================================================
    # Position management (orchestrator + helpers)
    # ==================================================================

    def _manage_position(self, symbol: str) -> None:
        mp = self._managed[symbol]
        candles = self._buffers[symbol][self._entry_interval]
        if not candles:
            return

        ind = self._entry_indicators(symbol)
        if ind is None:
            return  # not enough history yet — let the next tick try again

        self._update_streaks_and_touch(mp, ind)

        # Break-even move: if size has shrunk vs initial, a TP has filled (TP1 takes
        # the bigger slice and fills first in normal flow). Once any reduction is
        # observed and we haven't moved yet, push the stop to entry so the runner
        # can't turn this back into a loser.
        live_state = self.state_manager.get_state(symbol)
        if (not mp.stop_moved_to_be
                and live_state.position != Position.NONE
                and live_state.size > 0
                and live_state.size < mp.initial_qty):
            self._move_stop_to_break_even(mp, live_state)

        invalidation = self._check_trend_invalidation(mp, ind, symbol)
        if invalidation is not None:
            self._exit_position(symbol, invalidation)
            return

        candles_since_entry = sum(
            1 for c in candles if c["open_time"] > mp.entry_candle_open_time
        )
        time_exit = self._check_time_exit(mp, ind, candles_since_entry)
        if time_exit is not None:
            self._exit_position(symbol, time_exit)
            return

        self._log_managed_hold(mp, ind, candles_since_entry)

    def _move_stop_to_break_even(self, mp: _ManagedPosition, state) -> None:
        """Replace the layered stop with a new pair at entry (± optional ATR offset).

        Place-then-cancel so the position is never momentarily unprotected. Sized
        to the CURRENT remaining position (state.size), not the original qty —
        reduceOnly handles over-sizing on Binance's side anyway, but matching qty
        keeps the order log clean.
        """
        offset_mult = float(self.params.get("break_even_offset_atr_mult", 0.0))
        tick_size = self.sym_infos[mp.symbol]["tick_size"]
        if mp.side == "LONG":
            raw = float(mp.entry_price) + offset_mult * mp.entry_atr
            exit_side = "SELL"
        else:
            raw = float(mp.entry_price) - offset_mult * mp.entry_atr
            exit_side = "BUY"
        new_stop_price = round_price(Decimal(str(raw)), tick_size)

        self.state_manager.mark_change(mp.symbol)
        new_ids = self._replace_layered_stop(
            mp.symbol, exit_side, state.size, new_stop_price, mp.stop_ids,
        )
        if new_ids is None:
            logger.error("{} {} break-even stop placement failed — keeping old stop",
                         self.tag, mp.symbol)
            self.state_manager.mark_change(mp.symbol)
            return

        mp.stop_ids = new_ids
        mp.stop_moved_to_be = True
        self._persist_managed(mp.symbol)
        self.state_manager.mark_change(mp.symbol)
        logger.info("{} {} stop moved to break-even @ {} (qty={}, limit_id={}, market_id={})",
                    self.tag, mp.symbol, new_stop_price, state.size,
                    new_ids.limit_id, new_ids.market_id)

    def _update_streaks_and_touch(
        self, mp: _ManagedPosition, ind: _EntryIndicators,
    ) -> None:
        """Update outside-band / RSI-extreme streaks and the touched-middle flag.

        Streaks count CONSECUTIVE closed candles that close on the wrong side of
        the relevant band (or with RSI past its extreme). A single closed candle
        on the right side resets the streak to zero.
        """
        rsi_oversold = float(self.params.get("rsi_oversold", 30.0))
        rsi_overbought = float(self.params.get("rsi_overbought", 70.0))

        changed = False
        if mp.side == "LONG":
            new_band = mp.outside_band_streak + 1 if ind.close < ind.bb_lower else 0
            new_rsi = mp.rsi_extreme_streak + 1 if ind.rsi_now < rsi_oversold else 0
            new_touched = mp.touched_middle or (ind.close >= ind.bb_middle)
        else:
            new_band = mp.outside_band_streak + 1 if ind.close > ind.bb_upper else 0
            new_rsi = mp.rsi_extreme_streak + 1 if ind.rsi_now > rsi_overbought else 0
            new_touched = mp.touched_middle or (ind.close <= ind.bb_middle)

        if new_band != mp.outside_band_streak:
            mp.outside_band_streak = new_band
            changed = True
        if new_rsi != mp.rsi_extreme_streak:
            mp.rsi_extreme_streak = new_rsi
            changed = True
        if new_touched != mp.touched_middle:
            mp.touched_middle = new_touched
            changed = True
        if changed:
            self._persist_managed(mp.symbol)

    def _check_trend_invalidation(
        self, mp: _ManagedPosition, ind: _EntryIndicators, symbol: str,
    ) -> Optional[str]:
        """Return a short exit reason string if any invalidation condition is met."""
        p = self.params
        # 1. 4h ADX crossed into trending territory
        adx_min_trend = float(p.get("regime_adx_min_trend", 25.0))
        adx_4h = self._regime_adx_now(symbol)
        if adx_4h is not None and adx_4h > adx_min_trend:
            return f"regime trending (4h ADX={adx_4h:.2f} > {adx_min_trend})"
        # 2. Volatility expanding on entry tf
        atr_max_mult = float(p.get("atr_max_expansion_mult", 1.2))
        if ind.atr_sma > 0 and ind.atr_now > atr_max_mult * ind.atr_sma:
            return (f"ATR expanding (atr={ind.atr_now:.4f} > "
                    f"{atr_max_mult}*sma={atr_max_mult * ind.atr_sma:.4f})")
        # 3. N consecutive closes outside the relevant band
        max_outside = int(p.get("max_outside_band_candles", 2))
        if mp.outside_band_streak >= max_outside:
            return f"closes outside band {mp.outside_band_streak}>={max_outside}"
        # 4. N consecutive RSI extremes against the trade
        max_rsi = int(p.get("max_rsi_extreme_candles", 2))
        if mp.rsi_extreme_streak >= max_rsi:
            return f"RSI extreme streak {mp.rsi_extreme_streak}>={max_rsi}"
        # 5. Hard SL re-check on close
        stop_atr_mult = float(p.get("stop_atr_mult", 1.0))
        entry = float(mp.entry_price)
        if mp.side == "LONG" and ind.close < entry - stop_atr_mult * mp.entry_atr:
            return f"close below SL ({ind.close:.4f} < {entry - stop_atr_mult * mp.entry_atr:.4f})"
        if mp.side == "SHORT" and ind.close > entry + stop_atr_mult * mp.entry_atr:
            return f"close above SL ({ind.close:.4f} > {entry + stop_atr_mult * mp.entry_atr:.4f})"
        return None

    def _check_time_exit(
        self, mp: _ManagedPosition, ind: _EntryIndicators, candles_since_entry: int,
    ) -> Optional[str]:
        """Soft + hard time stops.

        Soft: after `time_exit_soft_candles`, exit IF the band-middle was never
        reached AND the current close is the wrong side of entry. The intent is
        "the trade simply isn't working" — using a position vs entry check is
        cleaner than an arbitrary fraction-of-R threshold.

        Hard: after `time_exit_hard_candles`, exit unconditionally — mean
        reversion trades that need this long are statistically dead.
        """
        p = self.params
        hard_max = int(p.get("time_exit_hard_candles", 16))
        soft_max = int(p.get("time_exit_soft_candles", 8))

        if candles_since_entry >= hard_max:
            return f"hard time exit ({candles_since_entry}>={hard_max} candles)"

        if candles_since_entry >= soft_max and not mp.touched_middle:
            entry = float(mp.entry_price)
            wrong_side = ((mp.side == "LONG" and ind.close < entry)
                          or (mp.side == "SHORT" and ind.close > entry))
            if wrong_side:
                return (f"soft time exit ({candles_since_entry}>={soft_max}, "
                        f"no mid-touch, close={ind.close:.4f} wrong side of entry={entry:.4f})")
        return None

    def _log_managed_hold(
        self, mp: _ManagedPosition, ind: _EntryIndicators, candles_since_entry: int,
    ) -> None:
        entry = float(mp.entry_price)
        pnl_per_unit = ((ind.close - entry) if mp.side == "LONG"
                        else (entry - ind.close))
        r_progress = pnl_per_unit / mp.r_distance if mp.r_distance > 0 else 0.0
        logger.info(
            "{} {} {} HOLD position — side={} close={:.4f} bb_mid={:.4f} R={:+.2f} "
            "RSI={:.1f} ATR={:.4f} outside_streak={} rsi_streak={} touched_mid={} "
            "candles_since_entry={}",
            self.tag, mp.symbol, self._entry_interval, mp.side, ind.close, ind.bb_middle,
            r_progress, ind.rsi_now, ind.atr_now,
            mp.outside_band_streak, mp.rsi_extreme_streak, mp.touched_middle,
            candles_since_entry,
        )

