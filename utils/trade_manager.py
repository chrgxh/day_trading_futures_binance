"""Background trade-state manager — monitors open positions and reconciles orders on external fills."""

import threading
import time
from dataclasses import dataclass
from decimal import Decimal

from binance.client import Client
from loguru import logger

from utils import account, algo_orders, orders
from utils.general import round_price
from utils.indicators import Position


@dataclass
class _TradeState:
    symbol: str
    position: Position
    size: Decimal
    entry_price: Decimal
    tick_size: Decimal
    stop_side: str
    stop_ids: list[int]
    sl_limit_price: Decimal
    sl_market_price: Decimal
    ttp_id: int | None
    tp_limit_id: int | None
    registered_at_ms: int        # epoch ms — used to bound the P&L query to this trade
    has_order_details: bool = True
    sl_moved: bool = False       # True once the SL milestone has fired — prevents repeated moves
    candle_count: int = 0        # incremented on every closed candle while in the trade
    checkpoint_price: Decimal = Decimal("0")  # price at last stagnation window boundary


class TradeManager:
    """Polls Binance every poll_interval_secs for each tracked symbol.

    Detects external position closes (stop fired, TP limit fully filled) and partial
    TP fills. On full close: identifies what fired, cancels remaining orders, verifies
    cancellation, and logs realized P&L. On partial fill: re-places stop orders at the
    reduced size and verifies the new orders are live.
    """

    def __init__(
        self,
        client: Client,
        poll_interval_secs: int = 10,
        sl_profit_trigger_pct: Decimal = Decimal("0.01"),
        sl_profit_lock_pct: Decimal = Decimal("0.005"),
        sl_profit_market_lock_pct: Decimal = Decimal("0.003"),
    ):
        self._client = client
        self._poll_interval = poll_interval_secs
        self._sl_profit_trigger_pct = sl_profit_trigger_pct
        self._sl_profit_lock_pct = sl_profit_lock_pct
        self._sl_profit_market_lock_pct = sl_profit_market_lock_pct
        self._states: dict[str, _TradeState] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="TradeManager")

    def start(self) -> None:
        """Start the background polling thread."""
        self._thread.start()
        logger.info(
            "TradeManager started (poll_interval={}s, sl_trigger={:.1f}%, sl_lock={:.1f}%, sl_market_lock={:.1f}%)",
            self._poll_interval,
            float(self._sl_profit_trigger_pct * 100),
            float(self._sl_profit_lock_pct * 100),
            float(self._sl_profit_market_lock_pct * 100),
        )

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it to exit."""
        self._stop_event.set()
        self._thread.join(timeout=15)
        logger.info("TradeManager stopped.")

    def register_trade(
        self,
        symbol: str,
        position: Position,
        size: Decimal,
        entry_price: Decimal,
        tick_size: Decimal,
        stop_ids: list[int],
        sl_limit_price: Decimal,
        sl_market_price: Decimal,
        ttp_id: int | None,
        tp_limit_id: int | None,
        has_order_details: bool = True,
    ) -> None:
        """Register an open trade for background monitoring.

        Args:
            symbol: Trading pair, e.g. "BTCUSDT".
            position: LONG or SHORT.
            size: Current position size in base asset units.
            entry_price: Average fill price.
            tick_size: Symbol tick size (used when re-placing stops after partial fills).
            stop_ids: [sl_limit_algo_id, sl_market_algo_id].
            sl_limit_price: Actual trigger/limit price of the stop-limit order.
            sl_market_price: Actual trigger price of the stop-market order.
            ttp_id: Trailing stop algo ID, or None.
            tp_limit_id: GTC TP limit order ID, or None.
            has_order_details: False when registering a recovered position whose order
                IDs are unknown (restart scenario). Disables stop re-placement on
                partial fills.
        """
        stop_side = "SELL" if position == Position.LONG else "BUY"
        with self._lock:
            self._states[symbol] = _TradeState(
                symbol=symbol,
                position=position,
                size=size,
                entry_price=entry_price,
                tick_size=tick_size,
                stop_side=stop_side,
                stop_ids=list(stop_ids),
                sl_limit_price=sl_limit_price,
                sl_market_price=sl_market_price,
                ttp_id=ttp_id,
                tp_limit_id=tp_limit_id,
                registered_at_ms=int(time.time() * 1000),
                has_order_details=has_order_details,
                checkpoint_price=entry_price,
            )
        logger.info(
            "TradeManager: registered {} {} size={} entry={} order_details={}",
            symbol, position.value, size, entry_price, has_order_details,
        )

    def close_trade(self, symbol: str) -> None:
        """Cancel all orders for the symbol and remove it from tracking.

        Called when the strategy issues a CLOSE signal so the background thread
        does not interfere with the orderly close sequence in execute_signal.

        Args:
            symbol: Trading pair to stop tracking.
        """
        with self._lock:
            state = self._states.pop(symbol, None)
        if state is None:
            logger.debug("TradeManager: close_trade({}) called but no active state.", symbol)
            return
        self._cancel_all_orders(state)
        logger.info("TradeManager: trade closed for {} (strategy signal).", symbol)

    def get_position(self, symbol: str) -> Position:
        """Current known position side for a symbol.

        Args:
            symbol: Trading pair, e.g. "BTCUSDT".

        Returns:
            Position.LONG, Position.SHORT, or Position.NONE if not tracked.
        """
        with self._lock:
            state = self._states.get(symbol)
            return state.position if state else Position.NONE

    def get_size(self, symbol: str) -> Decimal:
        """Current known position size for a symbol.

        Args:
            symbol: Trading pair, e.g. "BTCUSDT".

        Returns:
            Position size in base asset units, or Decimal("0") if not tracked.
        """
        with self._lock:
            state = self._states.get(symbol)
            return state.size if state else Decimal("0")

    def tick_stagnation(
        self,
        symbol: str,
        current_price: Decimal,
        current_adx: Decimal,
        current_rsi: Decimal,
        min_adx: Decimal,
        rsi_long_low: Decimal,
        rsi_short_high: Decimal,
        stagnation_candles: int,
        stagnation_min_pct: Decimal,
    ) -> bool:
        """Increment the candle counter and check for momentum stagnation.

        Called on every closed candle for symbols with an active position. Every
        stagnation_candles ticks, checks whether price has moved at least
        stagnation_min_pct in the trade's favour from the last checkpoint AND
        whether entry-quality indicator conditions still hold. Both conditions
        must fail simultaneously to trigger a close.

        On a passing window the checkpoint price is reset to current_price so
        the next window measures progress from here, not from entry.

        Args:
            symbol: Trading pair.
            current_price: Latest candle close price.
            current_adx: ADX value on the current candle.
            current_rsi: RSI value on the current candle.
            min_adx: Minimum ADX threshold (same value used at entry).
            rsi_long_low: RSI lower bound for long momentum zone (same as entry gate).
            rsi_short_high: RSI upper bound for short momentum zone (same as entry gate).
            stagnation_candles: Evaluate every N candles.
            stagnation_min_pct: Required price progress per window, in percent (e.g. 2.0 = 2%).

        Returns:
            True if stagnation is confirmed and the position should be closed.
        """
        with self._lock:
            state = self._states.get(symbol)
            if state is None:
                return False

            state.candle_count += 1
            if state.candle_count % stagnation_candles != 0:
                return False

            if state.position == Position.LONG:
                price_pct = (current_price - state.checkpoint_price) / state.checkpoint_price * 100
                rsi_weak = current_rsi < rsi_long_low
            else:
                price_pct = (state.checkpoint_price - current_price) / state.checkpoint_price * 100
                rsi_weak = current_rsi > rsi_short_high

            adx_weak = current_adx < min_adx

            if price_pct < stagnation_min_pct and adx_weak and rsi_weak:
                logger.info(
                    "TradeManager: {} stagnation detected at candle {} — "
                    "price moved {:.3f}% from checkpoint (need {:.1f}%), "
                    "ADX={:.1f} (below min {}), RSI={:.2f} (out of momentum zone) — closing.",
                    symbol, state.candle_count, float(price_pct), float(stagnation_min_pct),
                    float(current_adx), float(min_adx), float(current_rsi),
                )
                return True

            state.checkpoint_price = current_price
            logger.debug(
                "TradeManager: {} stagnation window {} passed — "
                "price_pct={:.3f}% adx_weak={} rsi_weak={} — checkpoint reset to {}.",
                symbol, state.candle_count // stagnation_candles,
                float(price_pct), adx_weak, rsi_weak, current_price,
            )
            return False

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                symbols = list(self._states.keys())
            for symbol in symbols:
                try:
                    self._reconcile(symbol)
                except Exception as exc:
                    logger.warning("TradeManager: error reconciling {}: {}", symbol, exc)
            self._stop_event.wait(self._poll_interval)

    def _reconcile(self, symbol: str) -> None:
        pos_list = account.get_futures_positions(self._client, symbol=symbol)
        binance_size = abs(pos_list[0]["amount"]) if pos_list else Decimal("0")

        with self._lock:
            state = self._states.get(symbol)
        if state is None:
            return

        if binance_size == 0:
            with self._lock:
                self._states.pop(symbol, None)
            self._handle_external_close(state)

        elif binance_size < state.size:
            self._handle_partial_fill(state, binance_size)

        else:
            if not state.sl_moved and state.has_order_details and pos_list:
                self._check_sl_milestone(state, pos_list[0]["unrealized_pnl"])
            logger.debug("TradeManager: {} position unchanged (size={}).", symbol, binance_size)

    # ------------------------------------------------------------------
    # External close handler
    # ------------------------------------------------------------------

    def _handle_external_close(self, state: _TradeState) -> None:
        """Position reached zero externally. Identify what fired, cancel leftovers, verify, log P&L."""
        open_ids = self._fetch_open_order_ids(state.symbol)

        fired_label = self._identify_fired_order(state, open_ids)
        leftover_ids = self._identify_leftover_order_ids(state, open_ids)

        logger.info(
            "TradeManager: {} {} position CLOSED externally. "
            "Triggered by: {}. Leftover orders to cancel: {}",
            state.symbol, state.position.value,
            fired_label,
            leftover_ids if leftover_ids else "none",
        )

        if leftover_ids:
            self._cancel_leftover_orders(state, leftover_ids)
            self._verify_orders_cancelled(state.symbol, leftover_ids)
        else:
            logger.info("TradeManager: {} no leftover orders to cancel.", state.symbol)

        pnl = self._query_realized_pnl(state)
        if pnl != 0:
            outcome = "WIN" if pnl > 0 else "LOSS"
            logger.info(
                "TradeManager: {} realized P&L: {:+.4f} USDT ({})",
                state.symbol, pnl, outcome,
            )
        else:
            logger.info("TradeManager: {} realized P&L: 0 or unavailable.", state.symbol)

    def _identify_fired_order(self, state: _TradeState, open_ids: set[int]) -> str:
        """Return a human-readable label for the exit order that fired."""
        if not state.has_order_details:
            return "unknown (recovered position — order IDs not tracked)"

        fired = []
        if state.tp_limit_id is not None and state.tp_limit_id not in open_ids:
            fired.append(f"TP limit (id={state.tp_limit_id})")
        if len(state.stop_ids) > 0 and state.stop_ids[0] not in open_ids:
            fired.append(f"stop-limit (id={state.stop_ids[0]})")
        if len(state.stop_ids) > 1 and state.stop_ids[1] not in open_ids:
            fired.append(f"stop-market (id={state.stop_ids[1]})")
        if state.ttp_id is not None and state.ttp_id not in open_ids:
            fired.append(f"trailing TP (id={state.ttp_id})")

        return ", ".join(fired) if fired else "unknown"

    def _identify_leftover_order_ids(self, state: _TradeState, open_ids: set[int]) -> list[int]:
        """Return IDs of tracked orders still open on Binance (need cancellation)."""
        leftover = []
        for oid in state.stop_ids:
            if oid in open_ids:
                leftover.append(oid)
        if state.ttp_id is not None and state.ttp_id in open_ids:
            leftover.append(state.ttp_id)
        if state.tp_limit_id is not None and state.tp_limit_id in open_ids:
            leftover.append(state.tp_limit_id)
        return leftover

    def _verify_orders_cancelled(self, symbol: str, expected_cancelled: list[int]) -> None:
        """Re-fetch open orders and log which IDs are still live vs confirmed cancelled."""
        remaining_ids = self._fetch_open_order_ids(symbol)
        still_open = [oid for oid in expected_cancelled if oid in remaining_ids]
        confirmed = [oid for oid in expected_cancelled if oid not in remaining_ids]

        if confirmed:
            logger.info("TradeManager: {} cancelled orders confirmed gone: {}", symbol, confirmed)
        if still_open:
            logger.warning(
                "TradeManager: {} orders still live after cancel attempt — check manually: {}",
                symbol, still_open,
            )

    # ------------------------------------------------------------------
    # Partial fill handler
    # ------------------------------------------------------------------

    def _handle_partial_fill(self, state: _TradeState, new_size: Decimal) -> None:
        """TP limit partially filled. Re-place stops at reduced size, verify, update state, log P&L."""
        filled_qty = state.size - new_size
        fill_pct = float(filled_qty / state.size * 100)

        logger.info(
            "TradeManager: {} partial TP limit fill — size {} → {} ({:.1f}% of position filled). "
            "Re-placing stops at new size.",
            state.symbol, state.size, new_size, fill_pct,
        )

        new_stop_ids = self._replace_stops(state, new_size)

        if new_stop_ids:
            self._verify_orders_placed(state.symbol, new_stop_ids)

        with self._lock:
            if state.symbol in self._states:
                self._states[state.symbol].size = new_size
                self._states[state.symbol].stop_ids = new_stop_ids

        pnl = self._query_realized_pnl(state)
        if pnl != 0:
            logger.info(
                "TradeManager: {} partial P&L so far (cumulative since open): {:+.4f} USDT",
                state.symbol, pnl,
            )

    def _verify_orders_placed(self, symbol: str, new_ids: list[int]) -> None:
        """Confirm newly placed stop orders appear in open orders."""
        open_ids = self._fetch_open_order_ids(symbol)
        confirmed = [oid for oid in new_ids if oid in open_ids]
        missing = [oid for oid in new_ids if oid not in open_ids]

        if confirmed:
            logger.info("TradeManager: {} new stop orders confirmed live: {}", symbol, confirmed)
        if missing:
            logger.warning(
                "TradeManager: {} new stop orders NOT found in open orders — check manually: {}",
                symbol, missing,
            )

    # ------------------------------------------------------------------
    # SL profit-lock milestone
    # ------------------------------------------------------------------

    def _check_sl_milestone(self, state: _TradeState, unrealized_pnl: Decimal) -> None:
        """Move stops to profit-lock levels once Binance's unrealized P&L crosses the trigger threshold.

        Uses the unrealized_pnl value from the position endpoint directly — authoritative and already
        accounts for funding fees. pnl_pct = unrealized_pnl / notional works for both LONG and SHORT
        since Binance's sign handles direction (positive = in profit, negative = in loss).
        """
        notional = state.size * state.entry_price
        if notional == 0:
            return
        pnl_pct = unrealized_pnl / notional

        if pnl_pct < self._sl_profit_trigger_pct:
            return

        if state.position == Position.LONG:
            new_sl_limit_price = round_price(
                state.entry_price * (1 + self._sl_profit_lock_pct), state.tick_size
            )
            new_sl_market_price = round_price(
                state.entry_price * (1 + self._sl_profit_market_lock_pct), state.tick_size
            )
        else:
            new_sl_limit_price = round_price(
                state.entry_price * (1 - self._sl_profit_lock_pct), state.tick_size
            )
            new_sl_market_price = round_price(
                state.entry_price * (1 - self._sl_profit_market_lock_pct), state.tick_size
            )

        logger.info(
            "TradeManager: {} unrealized P&L {:.3f}% >= trigger {:.3f}% — "
            "moving SL to profit-lock (limit={}, market={}).",
            state.symbol, float(pnl_pct * 100), float(self._sl_profit_trigger_pct * 100),
            new_sl_limit_price, new_sl_market_price,
        )
        self._move_stop_to_profit(state, new_sl_limit_price, new_sl_market_price)

    def _move_stop_to_profit(
        self,
        state: _TradeState,
        new_sl_limit_price: Decimal,
        new_sl_market_price: Decimal,
    ) -> None:
        """Place new profit-lock stops first, then cancel the old ones to avoid an unprotected window."""
        new_ids: list[int] = []

        try:
            sl_limit = algo_orders.place_stop_limit_order(
                self._client, state.symbol, state.stop_side, state.size,
                new_sl_limit_price, new_sl_limit_price,
            )
            new_ids.append(sl_limit["order_id"])
            logger.info(
                "TradeManager: {} new profit-lock stop-limit placed at {} id={}.",
                state.symbol, new_sl_limit_price, sl_limit["order_id"],
            )
        except Exception as exc:
            logger.error(
                "TradeManager: could not place profit-lock stop-limit for {}: {}", state.symbol, exc
            )

        try:
            sl_market = algo_orders.place_stop_market_order(
                self._client, state.symbol, state.stop_side, state.size, new_sl_market_price,
            )
            new_ids.append(sl_market["order_id"])
            logger.info(
                "TradeManager: {} new profit-lock stop-market placed at {} id={}.",
                state.symbol, new_sl_market_price, sl_market["order_id"],
            )
        except Exception as exc:
            logger.error(
                "TradeManager: could not place profit-lock stop-market for {}: {}", state.symbol, exc
            )

        # Cancel old stops only after new ones are live — no unprotected window.
        for algo_id in list(state.stop_ids):
            try:
                algo_orders.cancel_algo_order(self._client, state.symbol, algo_id)
                logger.info(
                    "TradeManager: {} cancelled old stop id={} after profit-lock move.", state.symbol, algo_id
                )
            except Exception as exc:
                logger.warning(
                    "TradeManager: could not cancel old stop {} for {}: {}", algo_id, state.symbol, exc
                )

        if new_ids:
            self._verify_orders_placed(state.symbol, new_ids)

        with self._lock:
            if state.symbol in self._states:
                s = self._states[state.symbol]
                s.sl_moved = True
                s.stop_ids = new_ids
                s.sl_limit_price = new_sl_limit_price
                s.sl_market_price = new_sl_market_price

        logger.info(
            "TradeManager: {} SL profit-lock complete — limit={} market={} new_ids={}.",
            state.symbol, new_sl_limit_price, new_sl_market_price, new_ids,
        )

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    def _fetch_open_order_ids(self, symbol: str) -> set[int]:
        """Return the set of order IDs currently open for the symbol."""
        try:
            open_orders = orders.get_open_orders(self._client, symbol)
            return {o["order_id"] for o in open_orders}
        except Exception as exc:
            logger.warning("TradeManager: could not fetch open orders for {}: {}", symbol, exc)
            return set()

    def _cancel_leftover_orders(self, state: _TradeState, leftover_ids: list[int]) -> None:
        """Cancel only the orders confirmed still open on Binance (identified leftovers).

        Routes each ID to algo_orders.cancel_algo_order or orders.cancel_order based
        on whether it is a conditional order or a regular limit order.
        """
        algo_ids = set(state.stop_ids)
        if state.ttp_id is not None:
            algo_ids.add(state.ttp_id)

        for oid in leftover_ids:
            if oid in algo_ids:
                try:
                    algo_orders.cancel_algo_order(self._client, state.symbol, oid)
                except Exception as exc:
                    logger.warning(
                        "TradeManager: could not cancel algo order {} for {}: {}", oid, state.symbol, exc
                    )
            else:
                try:
                    orders.cancel_order(self._client, state.symbol, oid)
                except Exception as exc:
                    logger.warning(
                        "TradeManager: could not cancel order {} for {}: {}", oid, state.symbol, exc
                    )

    def _cancel_all_orders(self, state: _TradeState) -> None:
        for algo_id in state.stop_ids:
            try:
                algo_orders.cancel_algo_order(self._client, state.symbol, algo_id)
            except Exception as exc:
                logger.warning(
                    "TradeManager: could not cancel stop {} for {}: {}", algo_id, state.symbol, exc
                )

        if state.ttp_id is not None:
            try:
                algo_orders.cancel_algo_order(self._client, state.symbol, state.ttp_id)
            except Exception as exc:
                logger.warning(
                    "TradeManager: could not cancel trailing TP {} for {}: {}", state.ttp_id, state.symbol, exc
                )

        if state.tp_limit_id is not None:
            try:
                orders.cancel_order(self._client, state.symbol, state.tp_limit_id)
            except Exception as exc:
                logger.warning(
                    "TradeManager: could not cancel TP limit {} for {}: {}", state.tp_limit_id, state.symbol, exc
                )

    def _replace_stops(self, state: _TradeState, new_size: Decimal) -> list[int]:
        """Cancel existing stop orders and re-place at the reduced size.

        Skipped when order details are unknown (restart-recovered positions).
        Returns the new list of stop algo IDs (empty if details unknown or placement failed).
        """
        if not state.has_order_details:
            logger.warning(
                "TradeManager: {} partial fill detected but order details unknown (recovered position) — "
                "stop quantities NOT adjusted. Check open orders manually.",
                state.symbol,
            )
            return []

        for algo_id in state.stop_ids:
            try:
                algo_orders.cancel_algo_order(self._client, state.symbol, algo_id)
                logger.info("TradeManager: {} cancelled old stop id={} for resize.", state.symbol, algo_id)
            except Exception as exc:
                logger.warning(
                    "TradeManager: could not cancel stop {} for resize: {}", algo_id, exc
                )

        new_ids: list[int] = []
        try:
            sl_limit = algo_orders.place_stop_limit_order(
                self._client, state.symbol, state.stop_side, new_size,
                state.sl_limit_price, state.sl_limit_price,
            )
            new_ids.append(sl_limit["order_id"])
        except Exception as exc:
            logger.error("TradeManager: could not re-place stop-limit for {}: {}", state.symbol, exc)

        try:
            sl_market = algo_orders.place_stop_market_order(
                self._client, state.symbol, state.stop_side, new_size, state.sl_market_price,
            )
            new_ids.append(sl_market["order_id"])
        except Exception as exc:
            logger.error("TradeManager: could not re-place stop-market for {}: {}", state.symbol, exc)

        return new_ids

    # ------------------------------------------------------------------
    # P&L
    # ------------------------------------------------------------------

    def _query_realized_pnl(self, state: _TradeState) -> Decimal:
        """Sum realized PnL from all closing-side fills since the trade was registered.

        Bounded by registered_at_ms so only fills belonging to this position are counted.
        """
        try:
            trades = account.get_futures_recent_trades(
                self._client, state.symbol, start_time_ms=state.registered_at_ms,
            )
            closing_side = "SELL" if state.position == Position.LONG else "BUY"
            return sum(t["realized_pnl"] for t in trades if t["side"] == closing_side)
        except Exception as exc:
            logger.warning("TradeManager: could not fetch realized P&L for {}: {}", state.symbol, exc)
            return Decimal("0")
