"""Trend-pullback strategy with a RESTING LIMIT entry (multi-timeframe).

This strategy is deliberately the LEAST restrictive of the three. The other two
are confirmation strategies — they wait for a candle to close proving the setup,
which stacks many gates and fires rarely. This one inverts that: it decides a
price level in advance and rests a passive limit order there, letting the market
come to it. Fewer gates, maker fills, more entries.

Two configurable intervals (both from `params` in config.yaml):
- `regime_interval` — trend filter; dictates long-or-short bias.
- `entry_interval`  — the resting limit level and all management fire on close.

Entry (longs; shorts inverse):
  Regime (regime_interval, e.g. 4h) — one loose gate:
    - EMA_fast > EMA_slow AND close > EMA_slow  → uptrend, longs only.
  Entry (entry_interval, e.g. 1h):
    - The pullback level = EMA_fast (optionally offset entry_offset_atr_mult * ATR
      further into the pullback).
    - Gate: current close must be on the FAR side of the level (close > level for
      longs) so the order rests as a maker order rather than filling as a taker.
    - A resting GTC LIMIT order is placed at the level via the base limit-entry
      helpers. It is registered as a pending entry — exempt from orphan
      cancellation; StateManager fires on_fill / on_cancel.

Resting order lifecycle (checked each closed entry-interval candle):
    - Regime flips against the order  → cancel.
    - Unfilled after entry_expiry_candles → cancel (re-evaluated fresh next tick).

On fill (StateManager on_fill callback, worker thread):
  SL/TP are placed off the AUTHORITATIVE fill price:
    R     = stop_atr_mult * entry_atr   (ATR frozen when the order was placed).
    Stop  = fill ∓ R, as a LAYERED pair (stop-limit + stop-market backstop).
    TP1   = fill ± tp1_r_multiple * R   (tp1_size_pct of qty, GTC reduce-only LIMIT).
    TP2   = fill ± tp2_r_multiple * R   (tp2_size_pct of qty, GTC reduce-only LIMIT;
                                         skipped if tp2_size_pct == 0).

Break-even SL move: the first closed candle on which the position size has shrunk
vs initial_qty (a TP partially filled) replaces the layered stop at entry
(± break_even_offset_atr_mult * ATR) for the remaining qty — place-then-cancel so
the position is never momentarily unprotected. Once TP1 fills the trade can no
longer become a net loser.

There is no trailing stop and no dead-trade gauntlet — the stop + two fixed
R-multiple TPs fully define each trade. This strategy does NOT use a
LiveTradeManager.

Restart recovery: `serialize_state` / `adopt` handle both states a store entry
can be in — status="pending" (re-arm the resting order, or open it if it filled
during downtime) and status="open" (reconcile the layered stop + TPs).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger

from core.strategies.base import LayeredStopIds, Strategy
from core.types import Action, Position, Signal, SymbolState
from utils import orders
from utils.general import round_price
from utils.indicators import atr, ema


@dataclass
class _PendingEntry:
    """A resting limit entry order with no position behind it yet."""

    symbol: str
    side: str                          # "LONG" or "SHORT"
    order_id: int
    price: Decimal                     # the resting limit price
    qty: Decimal
    entry_atr: float                   # ATR frozen at placement — drives R on fill
    placed_candle_open_time: int       # ms epoch of the candle that placed the order


@dataclass
class _ManagedPosition:
    """Per-position state tracked locally after the resting order fills."""

    symbol: str
    side: str                          # "LONG" or "SHORT"
    entry_price: Decimal               # authoritative avg fill price
    entry_atr: float                   # ATR frozen at order placement
    r_distance: float                  # stop_atr_mult * entry_atr (price-per-unit)
    initial_qty: Decimal
    entry_candle_open_time: int        # ms epoch of the candle the fill was seen on
    stop_moved_to_be: bool = False     # has the SL been moved to break-even?
    stop_ids: Optional[LayeredStopIds] = None
    tp1_order_id: Optional[int] = None
    tp2_order_id: Optional[int] = None


class TrendPullbackLimit(Strategy):
    """Trend-pullback strategy entering via a resting limit order at EMA_fast.

    Intervals (regime + entry) and every indicator period are configurable in
    params. See the module docstring.
    """

    _managed: dict[str, _ManagedPosition]  # narrows base's dict[str, Any]

    def __init__(self, *, params: dict, **kwargs) -> None:
        self._entry_interval = str(params.get("entry_interval", "1h"))
        self._regime_interval = str(params.get("regime_interval", "4h"))
        seen: set[str] = set()
        intervals: list[str] = []
        for iv in [self._entry_interval, self._regime_interval]:
            if iv not in seen:
                seen.add(iv)
                intervals.append(iv)
        super().__init__(intervals=intervals, params=params, **kwargs)
        # Resting limit orders awaiting a fill, keyed by symbol. Written from the
        # candle thread (placement) and from the StateManager worker thread
        # (on_fill / on_cancel callbacks) — see the threading note on _tick.
        self._pending: dict[str, _PendingEntry] = {}

    # ------------------------------------------------------------------
    # Warmup sizing
    # ------------------------------------------------------------------

    def candle_limit(self, interval: str) -> int:
        p = self.params
        needs: list[int] = [250]
        if interval == self._regime_interval:
            ema_slow = int(p.get("regime_ema_slow", 200))
            needs.append(ema_slow * 6 + 20)
        if interval == self._entry_interval:
            ema_fast = int(p.get("ema_fast", 20))
            atr_period = int(p.get("atr_period", 14))
            needs.extend([ema_fast * 6 + 20, atr_period + 30])
        return max(needs)

    # ------------------------------------------------------------------
    # Tick routing (overrides base)
    #
    # Threading note: on_fill / on_cancel run on the StateManager worker thread
    # and mutate self._pending / self._managed; _tick runs on the kline thread.
    # During the brief window inside a callback a symbol can be in neither dict —
    # the next closed candle re-evaluates and converges. No locking, matching the
    # rest of the codebase.
    # ------------------------------------------------------------------

    def _tick(self, symbol: str, interval: str) -> None:
        if interval != self._entry_interval:
            return  # regime interval only feeds its buffer

        self._sync_managed(symbol)

        if symbol in self._managed:
            self._manage_position(symbol)
            return

        if symbol in self._pending:
            self._manage_pending(symbol)
            return

        if self.state_manager.has_position(symbol):
            logger.info("{} {} {} NO-ENTRY foreign-position — symbol already held",
                        self.tag, symbol, self._entry_interval)
            return

        result = self._compute_entry(symbol)
        if result is None:
            return
        signal, entry_atr, level = result
        if not self.risk_guard.allow_open(symbol, self):
            logger.info("{} {} entry blocked by risk guard", self.tag, symbol)
            return
        self._place_resting_entry(signal, entry_atr, level)

    # ABC shims — kept concrete; the live flow runs through _tick + helpers.

    def compute_signal(self, symbol: str, candles: list[dict]) -> Optional[Signal]:
        result = self._compute_entry(symbol)
        return result[0] if result is not None else None

    def execute_open(self, signal: Signal) -> None:
        result = self._compute_entry(signal.symbol)
        if result is None:
            logger.warning("{} {} execute_open: entry no longer valid — skipping",
                            self.tag, signal.symbol)
            return
        _, entry_atr, level = result
        self._place_resting_entry(signal, entry_atr, level)

    # ==================================================================
    # Entry decision
    # ==================================================================

    def _regime_summary(self, symbol: str) -> tuple[bool, bool, str]:
        """Return (long_ok, short_ok, diagnostic) from the regime-interval buffer.

        One loose gate per side: an EMA stack with price beyond the slow EMA.
        long  → EMA_fast > EMA_slow AND close > EMA_slow.
        short → EMA_fast < EMA_slow AND close < EMA_slow.
        """
        p = self.params
        candles = self._buffers[symbol].get(self._regime_interval) or []
        ema_fast_p = int(p.get("regime_ema_fast", 50))
        ema_slow_p = int(p.get("regime_ema_slow", 200))

        needed = ema_slow_p + 10
        if len(candles) < needed:
            return False, False, f"warmup ({len(candles)}/{needed} candles)"

        closes = [c["close"] for c in candles]
        fast = ema(closes, ema_fast_p)
        slow = ema(closes, ema_slow_p)
        if not fast or not slow:
            return False, False, "ema-warmup"

        close_now = float(closes[-1])
        long_ok = fast[-1] > slow[-1] and close_now > slow[-1]
        short_ok = fast[-1] < slow[-1] and close_now < slow[-1]
        diag = (f"close={close_now:.4f} EMA{ema_fast_p}={fast[-1]:.4f} "
                f"EMA{ema_slow_p}={slow[-1]:.4f}")
        return long_ok, short_ok, diag

    def _entry_level_and_atr(self, symbol: str) -> Optional[tuple[float, float, float]]:
        """Return (pullback_level, atr_now, close_now) on the entry interval.

        The pullback level is EMA_fast, pushed entry_offset_atr_mult * ATR
        further into the pullback (below EMA for longs, above for shorts).
        None if the entry-interval buffer is not warmed up.
        """
        p = self.params
        candles = self._buffers[symbol][self._entry_interval]
        ema_fast_p = int(p.get("ema_fast", 20))
        atr_period = int(p.get("atr_period", 14))

        if len(candles) < max(ema_fast_p + 5, atr_period + 2):
            return None
        closes = [c["close"] for c in candles]
        ema_series = ema(closes, ema_fast_p)
        atr_series = atr(candles, atr_period)
        if not ema_series or not atr_series:
            return None
        return ema_series[-1], atr_series[-1], float(closes[-1])

    def _compute_entry(self, symbol: str) -> Optional[tuple[Signal, float, float]]:
        """Return (signal, entry_atr, resting_level) or None.

        The signal's stop/TP prices are provisional (computed off the level);
        they are recomputed off the authoritative fill price on on_fill.
        """
        long_ok, short_ok, regime_diag = self._regime_summary(symbol)
        if not long_ok and not short_ok:
            logger.info("{} {} {} NO-ENTRY regime — {}",
                        self.tag, symbol, self._entry_interval, regime_diag)
            return None

        lva = self._entry_level_and_atr(symbol)
        if lva is None:
            logger.info("{} {} {} NO-ENTRY warmup — entry indicators not ready",
                        self.tag, symbol, self._entry_interval)
            return None
        ema_now, atr_now, close_now = lva
        if atr_now <= 0:
            return None

        offset_mult = float(self.params.get("entry_offset_atr_mult", 0.0))
        is_long = long_ok
        if is_long:
            level = ema_now - offset_mult * atr_now
            if close_now <= level:
                logger.info("{} {} {} NO-ENTRY close below pullback level "
                            "(close={:.4f} level={:.4f}) — would fill as taker",
                            self.tag, symbol, self._entry_interval, close_now, level)
                return None
            action = Action.OPEN_LONG
        else:
            level = ema_now + offset_mult * atr_now
            if close_now >= level:
                logger.info("{} {} {} NO-ENTRY close above pullback level "
                            "(close={:.4f} level={:.4f}) — would fill as taker",
                            self.tag, symbol, self._entry_interval, close_now, level)
                return None
            action = Action.OPEN_SHORT

        signal = self._build_signal(symbol, action, level, atr_now, regime_diag)
        return signal, atr_now, level

    def _build_signal(
        self, symbol: str, action: Action, level: float, atr_now: float, regime_diag: str,
    ) -> Signal:
        p = self.params
        stop_atr_mult = float(p.get("stop_atr_mult", 1.5))
        tp1_r_multiple = float(p.get("tp1_r_multiple", 1.0))
        r = stop_atr_mult * atr_now
        if action == Action.OPEN_LONG:
            sl = level - r
            tp = level + tp1_r_multiple * r
            side_label = "LONG"
        else:
            sl = level + r
            tp = level - tp1_r_multiple * r
            side_label = "SHORT"
        reason = f"side={side_label} regime[{regime_diag}] ATR={atr_now:.4f}"
        logger.info("{} {} {} resting-entry signal — level={:.4f} sl~{:.4f} tp1~{:.4f}",
                    self.tag, symbol, side_label, level, sl, tp)
        return Signal(
            action=action, symbol=symbol, reason=reason,
            entry_price=Decimal(str(level)),
            stop_loss_price=Decimal(str(sl)),
            take_profit_price=Decimal(str(tp)),
        )

    # ==================================================================
    # Resting order placement + lifecycle
    # ==================================================================

    def _place_resting_entry(self, signal: Signal, entry_atr: float, level: float) -> None:
        symbol = signal.symbol
        sym_info = self.sym_infos[symbol]
        is_long = signal.action == Action.OPEN_LONG
        tick_size = sym_info["tick_size"]

        limit_price = round_price(Decimal(str(level)), tick_size)
        qty = self._compute_position_qty(limit_price, sym_info["step_size"])
        if qty <= 0:
            logger.warning("{} {} computed qty is zero (level={}) — skipping",
                            self.tag, symbol, limit_price)
            return

        side = "BUY" if is_long else "SELL"
        placed_open_time = self._buffers[symbol][self._entry_interval][-1]["open_time"]
        strategy_state = {
            "entry_atr": entry_atr,
            "placed_candle_open_time": placed_open_time,
        }
        order_id = self._place_limit_entry(
            symbol, side, qty, limit_price,
            on_fill=self._on_entry_fill,
            on_cancel=self._on_entry_cancel,
            strategy_state=strategy_state,
        )
        if order_id is None:
            return
        self._pending[symbol] = _PendingEntry(
            symbol=symbol,
            side="LONG" if is_long else "SHORT",
            order_id=order_id,
            price=limit_price,
            qty=qty,
            entry_atr=entry_atr,
            placed_candle_open_time=placed_open_time,
        )

    def _manage_pending(self, symbol: str) -> None:
        """Re-evaluate a resting limit entry on each closed entry-interval candle."""
        pe = self._pending[symbol]
        long_ok, short_ok, regime_diag = self._regime_summary(symbol)
        regime_supports = (pe.side == "LONG" and long_ok) or (pe.side == "SHORT" and short_ok)
        if not regime_supports:
            logger.info("{} {} resting entry cancelled — regime no longer supports {} ({})",
                        self.tag, symbol, pe.side, regime_diag)
            self._cancel_limit_entry(symbol, pe.order_id)
            self._pending.pop(symbol, None)
            return

        candles = self._buffers[symbol][self._entry_interval]
        elapsed = sum(1 for c in candles if c["open_time"] > pe.placed_candle_open_time)
        expiry = int(self.params.get("entry_expiry_candles", 3))
        if elapsed >= expiry:
            logger.info("{} {} resting entry expired unfilled ({}>={} candles) — cancelling",
                        self.tag, symbol, elapsed, expiry)
            self._cancel_limit_entry(symbol, pe.order_id)
            self._pending.pop(symbol, None)
            return

        logger.info("{} {} resting {} entry HOLD — @ {} ({}/{} candles elapsed)",
                    self.tag, symbol, pe.side, pe.price, elapsed, expiry)

    def _on_entry_cancel(self, symbol: str) -> None:
        """StateManager callback: the resting order vanished unfilled."""
        if self._pending.pop(symbol, None) is not None:
            logger.info("{} {} resting entry gone unfilled — pending state cleared",
                        self.tag, symbol)

    def _on_entry_fill(self, state: SymbolState) -> None:
        """StateManager callback: the resting order filled — a position now exists.

        Runs on the worker thread. Places the layered stop + TPs off the
        authoritative fill price and records the managed position.
        """
        symbol = state.symbol
        pe = self._pending.pop(symbol, None)
        if state.position == Position.NONE or state.size <= 0:
            logger.error("{} {} on_fill but no live position — ignoring", self.tag, symbol)
            return
        entry_atr = pe.entry_atr if pe is not None else self._latest_entry_atr(symbol)
        if entry_atr is None or entry_atr <= 0:
            logger.error("{} {} on_fill without a usable entry ATR — cannot place exits",
                          self.tag, symbol)
            self._emergency_close(symbol)
            return
        is_long = state.position == Position.LONG
        fill_price = state.entry_price if state.entry_price > 0 else (
            pe.price if pe is not None else state.mark_price)
        self._open_position(symbol, is_long, fill_price, state.size, entry_atr)

    def _open_position(
        self, symbol: str, is_long: bool, fill_price: Decimal,
        filled_qty: Decimal, entry_atr: float,
    ) -> None:
        """Place exits for a freshly-filled (or downtime-filled) entry and record it."""
        sym_info = self.sym_infos[symbol]
        tick_size = sym_info["tick_size"]
        stop_price, tp1_price, tp2_price, r_distance = self._compute_exit_prices(
            fill_price, entry_atr, is_long, tick_size,
        )

        exit_side = "SELL" if is_long else "BUY"
        tp1_qty, tp2_qty = self._split_tp_qty(filled_qty, sym_info["step_size"])
        tp1_id = self._maybe_place_tp_limit(symbol, exit_side, tp1_qty, tp1_price, "TP1")
        tp2_id = (self._maybe_place_tp_limit(symbol, exit_side, tp2_qty, tp2_price, "TP2")
                  if tp2_qty > 0 else None)

        stop_ids = self._place_layered_stop(symbol, exit_side, filled_qty, stop_price)
        if stop_ids is None:
            self._emergency_close(symbol)
            return

        entry_open_time = self._buffers[symbol][self._entry_interval][-1]["open_time"] \
            if self._buffers[symbol][self._entry_interval] else 0
        self._managed[symbol] = _ManagedPosition(
            symbol=symbol,
            side="LONG" if is_long else "SHORT",
            entry_price=fill_price,
            entry_atr=entry_atr,
            r_distance=r_distance,
            initial_qty=filled_qty,
            entry_candle_open_time=entry_open_time,
            stop_ids=stop_ids,
            tp1_order_id=tp1_id,
            tp2_order_id=tp2_id,
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
        self.state_manager.mark_change(symbol)
        logger.info("{} {} {} opened qty={} fill={} stop={} tp1={} tp2={} "
                    "(stop_limit_id={}, stop_market_id={}, tp1_id={}, tp2_id={})",
                    self.tag, symbol, "LONG" if is_long else "SHORT",
                    filled_qty, fill_price, stop_price, tp1_price, tp2_price,
                    stop_ids.limit_id, stop_ids.market_id, tp1_id, tp2_id)

    def _compute_exit_prices(
        self, fill_price: Decimal, entry_atr: float, is_long: bool, tick_size: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal, float]:
        """Return (stop_price, tp1_price, tp2_price, r_distance) off the fill price."""
        p = self.params
        stop_atr_mult = float(p.get("stop_atr_mult", 1.5))
        tp1_r = float(p.get("tp1_r_multiple", 1.0))
        tp2_r = float(p.get("tp2_r_multiple", 2.5))
        r_distance = stop_atr_mult * entry_atr
        r = Decimal(str(r_distance))
        if is_long:
            stop_price = round_price(fill_price - r, tick_size)
            tp1_price = round_price(fill_price + Decimal(str(tp1_r)) * r, tick_size)
            tp2_price = round_price(fill_price + Decimal(str(tp2_r)) * r, tick_size)
        else:
            stop_price = round_price(fill_price + r, tick_size)
            tp1_price = round_price(fill_price - Decimal(str(tp1_r)) * r, tick_size)
            tp2_price = round_price(fill_price - Decimal(str(tp2_r)) * r, tick_size)
        return stop_price, tp1_price, tp2_price, r_distance

    def _split_tp_qty(
        self, filled_qty: Decimal, step_size: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Allocate filled_qty across TP1 and TP2 by their size percentages."""
        tp1_pct = Decimal(str(self.params.get("tp1_size_pct", 0.5)))
        tp2_pct = Decimal(str(self.params.get("tp2_size_pct", 0.5)))
        tp1_qty = (filled_qty * tp1_pct // step_size) * step_size
        if tp2_pct <= 0:
            return tp1_qty, Decimal("0")
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

    def _orders_dict(self, symbol: str) -> dict:
        mp = self._managed.get(symbol)
        if mp is None:
            return {}
        limit_id = mp.stop_ids.limit_id if mp.stop_ids is not None else None
        market_id = mp.stop_ids.market_id if mp.stop_ids is not None else None
        return {
            "stop_limit_id": limit_id,
            "stop_market_id": market_id,
            "tp1_id": mp.tp1_order_id,
            "tp2_id": mp.tp2_order_id,
        }

    # ==================================================================
    # Position management — break-even move only
    # ==================================================================

    def _manage_position(self, symbol: str) -> None:
        mp = self._managed[symbol]
        live_state = self.state_manager.get_state(symbol)
        if (not mp.stop_moved_to_be
                and live_state.position != Position.NONE
                and live_state.size > 0
                and live_state.size < mp.initial_qty):
            self._move_stop_to_break_even(mp, live_state)
            return
        self._log_managed_hold(mp, live_state)

    def _move_stop_to_break_even(self, mp: _ManagedPosition, state: SymbolState) -> None:
        """Replace the layered stop at entry (± optional ATR offset) after a TP fill.

        Place-then-cancel so the position is never momentarily unprotected.
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

    def _log_managed_hold(self, mp: _ManagedPosition, state: SymbolState) -> None:
        candles = self._buffers[mp.symbol][self._entry_interval]
        close = float(candles[-1]["close"]) if candles else float(state.mark_price)
        entry = float(mp.entry_price)
        pnl_per_unit = (close - entry) if mp.side == "LONG" else (entry - close)
        r_progress = pnl_per_unit / mp.r_distance if mp.r_distance > 0 else 0.0
        logger.info("{} {} {} HOLD position — side={} close={:.4f} entry={:.4f} "
                    "R={:+.2f} size={} stop_at_be={}",
                    self.tag, mp.symbol, self._entry_interval, mp.side, close, entry,
                    r_progress, state.size, mp.stop_moved_to_be)

    # ==================================================================
    # Persistence (overrides for restart recovery)
    # ==================================================================

    def serialize_state(self, symbol: str) -> dict:
        mp = self._managed.get(symbol)
        if mp is None:
            return {}
        return {
            "entry_atr": mp.entry_atr,
            "r_distance": mp.r_distance,
            "entry_candle_open_time": mp.entry_candle_open_time,
            "stop_moved_to_be": mp.stop_moved_to_be,
        }

    def adopt(self, symbol: str, entry: dict) -> None:
        """Rehydrate state for `symbol` carried across restart.

        A store entry is either status="pending" (a resting limit order with no
        position yet) or status="open" (a live, managed position).
        """
        state = self.state_manager.get_state(symbol)
        if entry.get("status") == "pending":
            self._adopt_pending(symbol, entry, state)
        else:
            self._adopt_open(symbol, entry, state)

    def _adopt_pending(self, symbol: str, entry: dict, state: SymbolState) -> None:
        """Adopt a resting limit entry order persisted with status="pending"."""
        ss = entry.get("strategy_state") or {}
        try:
            entry_atr = float(ss["entry_atr"])
        except (KeyError, ValueError, TypeError):
            logger.error("{} {} adopt(pending) failed — missing entry_atr", self.tag, symbol)
            self.state_manager.clear_pending_entry(symbol)
            return
        placed_open_time = int(ss.get("placed_candle_open_time", 0))
        side_label = entry.get("side", "LONG")

        # Filled while the bot was down — open the position now (no exits exist yet).
        if state.position != Position.NONE and state.size > 0:
            logger.warning("{} {} resting entry filled during downtime — opening position",
                            self.tag, symbol)
            self.state_manager.clear_pending_entry(symbol)
            is_long = state.position == Position.LONG
            fill_price = state.entry_price if state.entry_price > 0 else state.mark_price
            self._open_position(symbol, is_long, fill_price, state.size, entry_atr)
            return

        order_id = (entry.get("orders") or {}).get("entry_id")
        live_ids = {o["order_id"] for o in state.orders}
        if order_id is not None and order_id in live_ids:
            try:
                price = Decimal(str(entry["entry_price"]))
                qty = Decimal(str(entry["qty"]))
            except (KeyError, ValueError):
                logger.error("{} {} adopt(pending) failed — bad price/qty", self.tag, symbol)
                self.state_manager.clear_pending_entry(symbol)
                return
            self._rearm_limit_entry(
                symbol, order_id, side_label, price, qty,
                on_fill=self._on_entry_fill, on_cancel=self._on_entry_cancel,
                strategy_state=ss,
            )
            self._pending[symbol] = _PendingEntry(
                symbol=symbol, side=side_label, order_id=order_id,
                price=price, qty=qty, entry_atr=entry_atr,
                placed_candle_open_time=placed_open_time,
            )
        else:
            logger.warning("{} {} resting entry gone during downtime — clearing pending",
                            self.tag, symbol)
            self.state_manager.clear_pending_entry(symbol)

    def _adopt_open(self, symbol: str, entry: dict, state: SymbolState) -> None:
        """Adopt a live managed position persisted with status="open"."""
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
            logger.error("{} {} adopt failed — missing entry_atr/r_distance", self.tag, symbol)
            return
        entry_candle_open_time = int(ss.get("entry_candle_open_time", 0))
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
            stop_moved_to_be=stop_moved_to_be,
            stop_ids=stop_ids,
            tp1_order_id=tp1_id,
            tp2_order_id=tp2_id,
        )
        self.state_manager.update_owner(symbol, orders=self._orders_dict(symbol))
