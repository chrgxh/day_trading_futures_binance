"""Strategy ABC.

A Strategy owns:
- a per-(symbol, interval) candle buffer (its private state)
- the signal computation (compute_signal)
- the execution mechanics (execute_open) — IOC, market, layered limits, etc.
- an optional LiveTradeManager for post-fill lifecycle (most strategies don't need it)

A strategy can subscribe to multiple intervals (e.g. a 4h regime filter + a 30m
execution timeframe). The bot is dumb routing only. It calls
strategy.on_candle(symbol, interval, candle); the strategy is responsible for
everything else, including consulting the StateManager to check whether the
symbol is held (one-position-per-symbol absolute, enforced via RiskGuard before
any entry).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger

from core.types import Position, Signal
from utils import algo_orders, orders, positions
from utils.general import round_price
from utils.indicators import atr


@dataclass
class LayeredStopIds:
    """The two algo-order IDs that together form one layered stop.

    `limit_id` — stop-limit (primary, intended fill at trigger price).
    `market_id` — stop-market backstop (fires further from entry, guarantees exit).
    Either may be None if a leg was never placed or has been cancelled/filled.
    """

    limit_id: Optional[int] = None
    market_id: Optional[int] = None

    def is_complete(self) -> bool:
        return self.limit_id is not None and self.market_id is not None

if TYPE_CHECKING:
    from core.risk_guard import RiskGuard
    from core.state_manager import StateManager
    from core.strategies.live_trade_manager import LiveTradeManager


class Strategy(ABC):
    """Per-strategy class. One instance handles all symbols at one or more intervals.

    Subclasses implement compute_signal and execute_open. Multi-interval strategies
    can override _tick(symbol, interval) for cross-interval routing.
    """

    def __init__(
        self,
        *,
        name: str,
        intervals: list[str],
        symbols: list[str],
        params: dict,
        client: Client,
        sym_infos: dict[str, dict],
        state_manager: "StateManager",
        risk_guard: "RiskGuard",
        live_trade_manager: Optional["LiveTradeManager"] = None,
    ) -> None:
        if not intervals:
            raise ValueError(f"Strategy {name!r} requires at least one interval")
        self.name = name
        self.intervals = list(intervals)
        self.symbols = symbols
        self.params = params
        self.client = client
        self.sym_infos = sym_infos
        self.state_manager = state_manager
        self.risk_guard = risk_guard
        self.live_trade_manager = live_trade_manager
        self._buffers: dict[str, dict[str, list[dict]]] = {
            s: {i: [] for i in self.intervals} for s in symbols
        }
        # Subclasses that manage post-fill lifecycle store per-symbol records
        # here (typed as their own `_ManagedPosition` dataclass). Shared helpers
        # in this base class assume entries expose at least an `initial_qty`
        # attribute.
        self._managed: dict[str, Any] = {}

        if live_trade_manager is not None:
            live_trade_manager.attach(self)
            state_manager.subscribe(live_trade_manager.on_state_update)

        # Register with the persistent ownership store so its entries are not
        # pruned as "unknown strategy" on poll.
        state_manager.attach_strategy(self.name)

    @property
    def tag(self) -> str:
        return f"[{self.name}]"

    def candle_limit(self, interval: str) -> int:
        """Number of warmup candles this strategy needs per (symbol, interval).

        Override to tune per-interval. Default 250 covers most indicator warmup needs.
        """
        return 250

    def warmup(self, symbol: str, interval: str, candles: list[dict]) -> None:
        """Seed the per-symbol, per-interval candle buffer with REST history."""
        self._buffers[symbol][interval] = list(candles)
        logger.info("{} {} {} warmup: {} candles", self.tag, symbol, interval, len(candles))

    def on_candle(self, symbol: str, interval: str, candle: dict) -> None:
        """Append the closed candle and run the strategy tick."""
        self._append_candle(symbol, interval, candle)
        try:
            self._tick(symbol, interval)
        except Exception as exc:
            logger.exception("{} {} {} tick error: {}", self.tag, symbol, interval, exc)

    def _append_candle(self, symbol: str, interval: str, candle: dict) -> None:
        buf = self._buffers[symbol][interval]
        if buf and candle["open_time"] == buf[-1]["open_time"]:
            buf[-1] = candle
        else:
            buf.append(candle)
            limit = self.candle_limit(interval)
            if len(buf) > limit:
                del buf[: len(buf) - limit]

    def _tick(self, symbol: str, interval: str) -> None:
        """Default tick: skip if symbol is held, else compute and act.

        Single-interval strategies can rely on this. Multi-interval strategies
        should override to coordinate across intervals.
        """
        # If any position exists on this symbol — opened by us, another strategy,
        # or pre-existing on restart — this strategy stays silent. One-per-symbol absolute.
        if self.state_manager.has_position(symbol):
            return

        signal = self.compute_signal(symbol, self._buffers[symbol][interval])
        if signal is None:
            return
        logger.info("{} {} {} {} — {}", self.tag, symbol, interval, signal.action.value, signal.reason)

        from core.types import Action
        if signal.action not in (Action.OPEN_LONG, Action.OPEN_SHORT):
            return

        if not self.risk_guard.allow_open(symbol, self):
            return

        self.execute_open(signal)

    @abstractmethod
    def compute_signal(self, symbol: str, candles: list[dict]) -> Optional[Signal]:
        """Return a Signal or None (None == do nothing this tick)."""

    @abstractmethod
    def execute_open(self, signal: Signal) -> None:
        """Open a position for the signal. Calls broker primitives directly.

        Implementation is fully strategy-owned — IOC, market, layered limits, whatever.
        After fill: place exit orders using signal.stop_loss_price / signal.take_profit_price,
        then register the trade with self.live_trade_manager (if attached).
        """

    # ------------------------------------------------------------------
    # Shared post-fill lifecycle helpers
    #
    # Strategies that manage their own positions (record local state, place
    # exit orders, replace stops, etc.) share a number of small primitives.
    # They live here so each strategy doesn't reimplement them; subclasses
    # that don't manage positions simply never call them.
    # ------------------------------------------------------------------

    def _compute_position_qty(self, entry_price: Decimal, step_size: Decimal) -> Decimal:
        """qty = floor(notional / entry_price, step_size)."""
        notional = Decimal(str(self.params.get("notional_per_trade_usdt", 100)))
        return (notional / entry_price // step_size) * step_size

    def _latest_entry_atr(self, symbol: str) -> Optional[float]:
        """Latest ATR on the entry interval, or None if not warmed up.

        Requires subclass to set `self._entry_interval` and to honour
        `params["atr_period"]` (default 14).
        """
        candles = self._buffers[symbol][self._entry_interval]
        atr_period = int(self.params.get("atr_period", 14))
        series = atr(candles, atr_period)
        return series[-1] if series else None

    # ------------------------------------------------------------------
    # Layered stop primitives
    #
    # Every protective stop is a PAIR:
    #   - A stop-limit at the configured stop_price (preferred fill; pays nothing
    #     beyond slippage to the limit price).
    #   - A stop-market backstop `stop_market_backstop_pct` further from entry
    #     (guarantees exit if the limit gets skipped or sits unfilled).
    #
    # Both legs are reduceOnly, sized to the full position qty. Reduce-only caps
    # the second leg automatically if the first partially fills, so partial fills
    # are handled by the exchange.
    #
    # Tunable per-strategy in `params`:
    #   stop_limit_buffer_pct  — limit price offset vs trigger (% of price).
    #                            0.0 → limit at trigger; positive → limit slightly
    #                            past trigger (more aggressive, more likely to fill).
    #   stop_market_backstop_pct — backstop trigger offset vs primary stop (% of price).
    # ------------------------------------------------------------------

    def _layered_stop_pcts(self) -> tuple[Decimal, Decimal]:
        """Return (limit_buffer_pct, backstop_offset_pct) as Decimals.

        Both are converted from percent values (e.g. 0.1 → 0.001).
        """
        limit_buffer = Decimal(str(self.params.get("stop_limit_buffer_pct", 0.0))) / Decimal("100")
        backstop = Decimal(str(self.params.get("stop_market_backstop_pct", 0.1))) / Decimal("100")
        return limit_buffer, backstop

    def _compute_layered_stop_prices(
        self, *, stop_price: Decimal, exit_side: str, tick_size: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Derive (limit_trigger, limit_price, market_trigger) from the desired stop.

        `exit_side` is the side of the protective orders ("SELL" closes a LONG,
        "BUY" closes a SHORT). For SELL (long protection, stop below price), the
        backstop sits BELOW stop_price; for BUY it sits ABOVE.
        """
        limit_buffer_pct, backstop_pct = self._layered_stop_pcts()
        limit_trigger = stop_price
        if exit_side == "SELL":
            limit_price = round_price(stop_price * (Decimal("1") - limit_buffer_pct), tick_size)
            market_trigger = round_price(stop_price * (Decimal("1") - backstop_pct), tick_size)
        else:
            limit_price = round_price(stop_price * (Decimal("1") + limit_buffer_pct), tick_size)
            market_trigger = round_price(stop_price * (Decimal("1") + backstop_pct), tick_size)
        return limit_trigger, limit_price, market_trigger

    def _place_layered_stop(
        self, symbol: str, exit_side: str, qty: Decimal, stop_price: Decimal,
    ) -> Optional[LayeredStopIds]:
        """Place a stop-limit + stop-market backstop pair.

        On success returns a fully-populated `LayeredStopIds`. On failure of
        EITHER leg, cancels any successfully-placed leg and returns None so the
        caller can emergency-close (the position would otherwise be unprotected).
        """
        tick_size = self.sym_infos[symbol]["tick_size"]
        limit_trigger, limit_price, market_trigger = self._compute_layered_stop_prices(
            stop_price=stop_price, exit_side=exit_side, tick_size=tick_size,
        )
        try:
            sl = algo_orders.place_stop_limit_order(
                self.client, symbol, exit_side, qty, limit_trigger, limit_price,
            )
        except (BinanceAPIException, BinanceRequestException) as exc:
            logger.error("{} {} stop-limit placement failed @ trigger={} limit={}: {}",
                         self.tag, symbol, limit_trigger, limit_price, exc)
            return None
        try:
            sm = algo_orders.place_stop_market_order(
                self.client, symbol, exit_side, qty, market_trigger,
            )
        except (BinanceAPIException, BinanceRequestException) as exc:
            logger.error("{} {} stop-market backstop placement failed @ {}: {} — cancelling stop-limit {}",
                         self.tag, symbol, market_trigger, exc, sl["order_id"])
            try:
                algo_orders.cancel_algo_order(self.client, symbol, sl["order_id"])
            except (BinanceAPIException, BinanceRequestException) as cancel_exc:
                logger.warning("{} {} could not cancel orphaned stop-limit {}: {}",
                               self.tag, symbol, sl["order_id"], cancel_exc)
            return None
        return LayeredStopIds(limit_id=sl["order_id"], market_id=sm["order_id"])

    def _cancel_layered_stop(self, symbol: str, ids: Optional[LayeredStopIds]) -> None:
        """Best-effort cancellation of both legs. Logs warnings on failure."""
        if ids is None:
            return
        for label, oid in (("stop-limit", ids.limit_id), ("stop-market", ids.market_id)):
            if oid is None:
                continue
            try:
                algo_orders.cancel_algo_order(self.client, symbol, oid)
            except (BinanceAPIException, BinanceRequestException) as exc:
                logger.warning("{} {} could not cancel {} {}: {}",
                               self.tag, symbol, label, oid, exc)

    def _replace_layered_stop(
        self, symbol: str, exit_side: str, qty: Decimal,
        new_stop_price: Decimal, old_ids: Optional[LayeredStopIds],
    ) -> Optional[LayeredStopIds]:
        """Place a new layered pair, then cancel the old. Never momentarily unprotected.

        Returns the new ids on success; returns None on failure with the old
        orders left in place (caller keeps using `old_ids`).
        """
        new_ids = self._place_layered_stop(symbol, exit_side, qty, new_stop_price)
        if new_ids is None:
            return None
        self._cancel_layered_stop(symbol, old_ids)
        return new_ids

    def _emergency_close(self, symbol: str) -> None:
        """Market-close a position when no protective stop is in place."""
        logger.error("{} {} no stop in place — emergency-closing position", self.tag, symbol)
        try:
            positions.close_position(self.client, symbol)
        except Exception as exc:
            logger.critical("{} {} emergency close ALSO failed: {}", self.tag, symbol, exc)
        self.state_manager.mark_change(symbol)

    def _persist_managed(self, symbol: str) -> None:
        """Push the latest in-memory state for `symbol` to the persistent store."""
        if symbol not in self._managed:
            return
        self.state_manager.update_owner(
            symbol,
            strategy_state=self.serialize_state(symbol),
            orders=self._orders_dict(symbol),
            qty=self._managed[symbol].initial_qty,
        )

    def _orders_dict(self, symbol: str) -> dict:
        """Strategy-specific order-id snapshot for the persistent store.

        Default is empty; subclasses that track exit orders override.
        """
        return {}

    def _sync_managed(self, symbol: str) -> None:
        """Drop the managed entry if the live position is gone (stop hit / manual close)."""
        if symbol not in self._managed:
            return
        state = self.state_manager.get_state(symbol)
        if state.position == Position.NONE:
            logger.info("{} {} managed position closed externally — clearing local state",
                        self.tag, symbol)
            self._managed.pop(symbol, None)

    def _adopt_replace_layered_stop(
        self, symbol: str, side: str, qty: Decimal,
        entry_price: Decimal, r_distance: float,
        survivor_ids: Optional[LayeredStopIds] = None,
    ) -> Optional[LayeredStopIds]:
        """Re-place a missing layered stop on adopt at entry_price ± r_distance.

        If `survivor_ids` is provided, any surviving leg is cancelled first so the
        adopt always produces a fresh, correctly-sized pair (no risk of a stale
        leg sized to the original qty drifting against a partially-filled position).
        """
        is_long = side == "LONG"
        tick_size = self.sym_infos[symbol]["tick_size"]
        if is_long:
            stop_price = round_price(entry_price - Decimal(str(r_distance)), tick_size)
        else:
            stop_price = round_price(entry_price + Decimal(str(r_distance)), tick_size)
        exit_side = "SELL" if is_long else "BUY"
        self.state_manager.mark_change(symbol)
        if survivor_ids is not None:
            self._cancel_layered_stop(symbol, survivor_ids)
        ids = self._place_layered_stop(symbol, exit_side, qty, stop_price)
        if ids is None:
            logger.error("{} {} adopt: could not re-place layered stop @ {}",
                         self.tag, symbol, stop_price)
            return None
        logger.warning("{} {} adopt: layered stop missing — placed new pair @ {} (limit_id={}, market_id={})",
                       self.tag, symbol, stop_price, ids.limit_id, ids.market_id)
        return ids

    def _exit_position(self, symbol: str, reason: str) -> None:
        """Cancel all orders + market-close + drop local managed entry."""
        mp = self._managed.get(symbol)
        if mp is None:
            return
        self.state_manager.mark_change(symbol)
        try:
            orders.cancel_all_orders(self.client, symbol)
        except Exception as exc:
            logger.warning("{} {} could not cancel orders before exit: {}", self.tag, symbol, exc)
        try:
            positions.close_position(self.client, symbol)
            logger.info("{} {} position closed via {}", self.tag, symbol, reason)
        except Exception as exc:
            logger.error("{} {} {} close failed: {}", self.tag, symbol, reason, exc)
        finally:
            self._managed.pop(symbol, None)
            self.state_manager.mark_change(symbol)

    # ------------------------------------------------------------------
    # Persistence hooks (override if the strategy needs restart recovery)
    # ------------------------------------------------------------------

    def serialize_state(self, symbol: str) -> dict:
        """Return a JSON-serializable blob of strategy-specific state for `symbol`.

        Stored under `strategy_state` in `state/positions.json`. The default
        returns an empty dict — strategies that need restart recovery override.
        """
        return {}

    def adopt(self, symbol: str, entry: dict) -> None:
        """Rehydrate internal state for a position carried across restart.

        `entry` is the persisted dict from PositionStore (keys: strategy, side,
        entry_price, qty, strategy_state, orders, opened_at). The default is a
        no-op — strategies that adopt override.
        """
        return

    def adopt_pre_existing(self) -> None:
        """Iterate persisted owner entries and adopt the ones owned by this strategy.

        Called by `bot.py` between strategy construction and `state_manager.start()`.
        Subclasses normally don't need to override — they override `adopt` instead.
        """
        for symbol in self.symbols:
            entry = self.state_manager.get_owner(symbol)
            if entry is None or entry.get("strategy") != self.name:
                continue
            try:
                self.adopt(symbol, entry)
                logger.info("{} {} adopted from persisted state (side={}, qty={})",
                            self.tag, symbol, entry.get("side"), entry.get("qty"))
            except Exception as exc:
                logger.exception("{} {} adopt failed: {}", self.tag, symbol, exc)
