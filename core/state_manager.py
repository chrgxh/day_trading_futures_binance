"""StateManager — WebSocket-driven source of truth for live state.

An authenticated Binance user-data WebSocket (UserDataStream) pushes account and
order events in real time. On every relevant event the affected symbol is
REST-refreshed (one position + open-orders snapshot) so `state.orders` is always
an authoritative snapshot rather than an incrementally-reconstructed list.

A low-frequency full REST resync runs as a safety net: it corrects any drift
from dropped events and always runs once after a (re)connect to cover the gap
while the socket was down.

Order/position matching on every refresh:
  - position with no exit orders   → warn (does not try to manage; user/owning strategy decides)
  - orders with no position        → cancel and warn (orphans)

A short grace period suppresses warnings/cancellations right after a strategy has
placed orders or closed a position, to avoid false-positive orphan flags during
the brief window between order placement and the next refresh.

Daily P&L and trade count are refreshed on every fill event and on every resync.
Optionally drives DailyPnLReporter for midnight CSV + email reports.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from binance.client import Client
from loguru import logger

from core.position_store import PositionStore
from core.types import Position, SymbolState
from utils import account, algo_orders, orders
from utils.user_stream import UserDataStream

# Sentinel pushed onto the event queue after every WS (re)connect — tells the
# worker to run a full REST resync covering anything missed while disconnected.
_RESYNC_EVENT = "_RESYNC"


class StateManager:
    """WebSocket-driven authoritative live state."""

    def __init__(
        self,
        client: Client,
        symbols: list[str],
        *,
        testnet: bool = False,
        resync_interval_secs: int = 90,
        grace_period_secs: int = 15,
        pnl_reporter: Optional["DailyPnLReporter"] = None,  # noqa: F821 (forward)
        positions_file: Path | str | None = None,
    ) -> None:
        self._client = client
        self._symbols = list(symbols)
        self._resync_interval = resync_interval_secs
        self._grace_period_secs = grace_period_secs
        self._pnl_reporter = pnl_reporter

        self._states: dict[str, SymbolState] = {
            s: SymbolState(symbol=s, position=Position.NONE, size=Decimal("0"),
                           entry_price=Decimal("0"), mark_price=Decimal("0"),
                           unrealized_pnl=Decimal("0"), orders=[])
            for s in self._symbols
        }
        self._grace_until: dict[str, float] = {}
        self._subscribers: list[Callable[[SymbolState], None]] = []

        self._daily_pnl: Decimal = Decimal("0")
        self._trade_count: int = 0
        self._daily_pnl_day: str = ""  # YYYY-MM-DD UTC of last refresh

        # Persistent ownership store. Loaded eagerly so strategies can call
        # get_owner() during adopt_pre_existing before start() runs.
        self._store: PositionStore | None = None
        if positions_file is not None:
            self._store = PositionStore(positions_file)
            self._store.load()
        self._known_strategies: set[str] = set()

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_resync = 0.0
        self._event_queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="state-manager")
        self._user_stream = UserDataStream(
            client, testnet,
            on_event=self._event_queue.put,
            on_connect=lambda: self._event_queue.put({"e": _RESYNC_EVENT}),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        # Run one synchronous resync before launching threads so callers see
        # accurate state immediately (e.g. positions recovered from a restart).
        try:
            self._resync()
            self._refresh_daily_pnl()
        except Exception as exc:
            logger.warning("[state] initial resync failed: {}", exc)
        self._last_resync = time.time()
        self._worker.start()
        self._user_stream.start()
        logger.info("[state] started (ws user-stream, resync={}s, grace={}s, symbols={})",
                    self._resync_interval, self._grace_period_secs, self._symbols)
        if self._pnl_reporter is not None:
            self._pnl_reporter.start()

    def stop(self) -> None:
        self._stop.set()
        self._user_stream.stop()
        self._worker.join(timeout=15)
        logger.info("[state] stopped")

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[SymbolState], None]) -> None:
        """Register a callback fired for a symbol whenever its state is refreshed."""
        with self._lock:
            self._subscribers.append(callback)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get_state(self, symbol: str) -> SymbolState:
        with self._lock:
            return self._states[symbol]

    def has_position(self, symbol: str) -> bool:
        with self._lock:
            state = self._states.get(symbol)
            return state is not None and state.position != Position.NONE

    def open_position_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._states.values() if s.position != Position.NONE)

    def daily_pnl(self) -> Decimal:
        with self._lock:
            return self._daily_pnl

    def trade_count(self) -> int:
        with self._lock:
            return self._trade_count

    # ------------------------------------------------------------------
    # Grace period
    # ------------------------------------------------------------------

    def mark_change(self, symbol: str) -> None:
        """Strategies call this when they place or cancel orders to suppress orphan
        warnings/cancellations on the next refresh for `grace_period_secs` seconds."""
        with self._lock:
            self._grace_until[symbol] = time.time() + self._grace_period_secs

    def _in_grace(self, symbol: str) -> bool:
        return time.time() < self._grace_until.get(symbol, 0.0)

    # ------------------------------------------------------------------
    # Persistent ownership (positions.json)
    # ------------------------------------------------------------------

    def attach_strategy(self, name: str) -> None:
        """Mark a strategy as currently configured. Owners not in this set are
        pruned from the persistent store (their position is left untouched on
        Binance — see [state] log line)."""
        with self._lock:
            self._known_strategies.add(name)

    def get_owner(self, symbol: str) -> Optional[dict[str, Any]]:
        """Return the persisted ownership entry for `symbol`, or None.

        Strategies call this in `adopt_pre_existing` to recover the state they
        were managing before restart.
        """
        if self._store is None:
            return None
        with self._lock:
            return self._store.get(symbol)

    def register_owner(
        self,
        symbol: str,
        *,
        strategy_name: str,
        side: str,
        entry_price: Any,
        qty: Any,
        strategy_state: dict[str, Any],
        orders: dict[str, Any],
    ) -> None:
        """Record that `strategy_name` owns the position on `symbol`. Persists immediately."""
        if self._store is None:
            return
        with self._lock:
            self._store.upsert(
                symbol,
                strategy=strategy_name, side=side,
                entry_price=entry_price, qty=qty,
                strategy_state=strategy_state, orders=orders,
            )
            self._store.save()

    def update_owner(
        self,
        symbol: str,
        *,
        strategy_state: dict[str, Any] | None = None,
        orders: dict[str, Any] | None = None,
        qty: Any | None = None,
    ) -> None:
        """Patch a subset of fields on the owner entry. No-op if symbol absent."""
        if self._store is None:
            return
        with self._lock:
            self._store.patch(symbol, strategy_state=strategy_state, orders=orders, qty=qty)
            self._store.save()

    def unregister_owner(self, symbol: str) -> None:
        if self._store is None:
            return
        with self._lock:
            if self._store.remove(symbol):
                self._store.save()

    def _prune_and_save_store(self) -> None:
        """Drop entries whose position no longer exists on Binance or whose
        strategy is no longer configured. Then persist."""
        if self._store is None:
            return
        with self._lock:
            for symbol, entry in self._store.all().items():
                state = self._states.get(symbol)
                if state is not None and state.position == Position.NONE:
                    if self._store.remove(symbol):
                        logger.info("[state] {} position closed — dropped owner entry", symbol)
                    continue
                owner_strategy = entry.get("strategy")
                if owner_strategy not in self._known_strategies:
                    if self._store.remove(symbol):
                        logger.warning(
                            "[state] {} owner strategy {!r} not registered — dropped entry; "
                            "position left untracked on Binance",
                            symbol, owner_strategy,
                        )
            self._store.save()

    # ------------------------------------------------------------------
    # Worker loop — drains WS events, runs the periodic safety-net resync
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._event_queue.get(timeout=1.0)
            except queue.Empty:
                event = None
            if self._stop.is_set():
                break
            try:
                if event is not None:
                    self._handle_event(event)
                if time.time() - self._last_resync >= self._resync_interval:
                    self._resync()
                    self._refresh_daily_pnl()
                    self._last_resync = time.time()
            except Exception as exc:
                logger.exception("[state] worker error: {}", exc)

    def _handle_event(self, event: dict) -> None:
        """Dispatch one user-data event: REST-refresh the affected symbol(s)."""
        etype = event.get("e")
        if etype == _RESYNC_EVENT:
            self._resync()
            self._refresh_daily_pnl()
            self._last_resync = time.time()
            return

        symbols: set[str] = set()
        refresh_pnl = False
        if etype == "ACCOUNT_UPDATE":
            for p in event.get("a", {}).get("P", []):
                symbols.add(p.get("s"))
        elif etype == "ORDER_TRADE_UPDATE":
            o = event.get("o", {})
            symbols.add(o.get("s"))
            if o.get("x") == "TRADE":  # an actual fill — realized P&L moved
                refresh_pnl = True
        else:
            return  # ACCOUNT_CONFIG_UPDATE / MARGIN_CALL / etc. — nothing to do

        for symbol in symbols:
            if symbol in self._states:
                self._refresh_symbol(symbol)
        if refresh_pnl:
            self._refresh_daily_pnl()

    # ------------------------------------------------------------------
    # State refresh — REST snapshots
    # ------------------------------------------------------------------

    def _resync(self) -> None:
        """Full REST snapshot of every symbol — startup + periodic safety net."""
        try:
            positions_list = account.get_futures_positions(self._client)
        except Exception as exc:
            logger.warning("[state] resync could not fetch positions: {}", exc)
            return
        pos_by_sym = {p["symbol"]: p for p in positions_list}

        for symbol in self._symbols:
            try:
                open_orders = orders.get_open_orders(self._client, symbol)
            except Exception as exc:
                logger.warning("[state] {} could not fetch open orders: {}", symbol, exc)
                continue
            self._apply_symbol_state(symbol, pos_by_sym.get(symbol), open_orders)

        self._prune_and_save_store()

    def _refresh_symbol(self, symbol: str) -> None:
        """REST-refresh a single symbol's position + open orders (event-driven)."""
        try:
            positions_list = account.get_futures_positions(self._client, symbol)
        except Exception as exc:
            logger.warning("[state] {} could not fetch position: {}", symbol, exc)
            return
        try:
            open_orders = orders.get_open_orders(self._client, symbol)
        except Exception as exc:
            logger.warning("[state] {} could not fetch open orders: {}", symbol, exc)
            return
        pos_data = next((p for p in positions_list if p["symbol"] == symbol), None)
        self._apply_symbol_state(symbol, pos_data, open_orders)
        self._prune_and_save_store()

    def _apply_symbol_state(
        self, symbol: str, pos_data: dict | None, open_orders: list[dict]
    ) -> None:
        """Commit a freshly-fetched snapshot for one symbol: store it, log the
        diff, reconcile orphans, notify subscribers."""
        if pos_data is not None:
            state = SymbolState(
                symbol=symbol,
                position=Position.LONG if pos_data["amount"] > 0 else Position.SHORT,
                size=abs(pos_data["amount"]),
                entry_price=pos_data["entry_price"],
                mark_price=pos_data["mark_price"],
                unrealized_pnl=pos_data["unrealized_pnl"],
                orders=open_orders,
            )
        else:
            state = SymbolState(
                symbol=symbol, position=Position.NONE, size=Decimal("0"),
                entry_price=Decimal("0"), mark_price=Decimal("0"),
                unrealized_pnl=Decimal("0"), orders=open_orders,
            )

        with self._lock:
            prev = self._states.get(symbol)
            self._states[symbol] = state
            subscribers = list(self._subscribers)

        self._log_diff(prev, state)

        if not self._in_grace(symbol):
            self._reconcile_orders(state)

        for cb in subscribers:
            try:
                cb(state)
            except Exception as exc:
                logger.warning("[state] subscriber error for {}: {}", symbol, exc)

    def _log_diff(self, prev: SymbolState | None, new: SymbolState) -> None:
        """Log only when something meaningful changed."""
        if prev is None:
            # First-ever snapshot for this symbol. Log only if it's not the boring "flat + no orders" case.
            if new.position != Position.NONE or new.orders:
                logger.info("[state] {} initial: position={} size={} entry={} orders={}",
                            new.symbol, new.position.value, new.size, new.entry_price, len(new.orders))
            return
        if prev.position != new.position:
            logger.info("[state] {} position {} -> {} (size={} entry={} orders={})",
                        new.symbol, prev.position.value, new.position.value,
                        new.size, new.entry_price, len(new.orders))
            return
        # Position unchanged — only flag order-count changes while a position is open.
        if new.position != Position.NONE and len(prev.orders) != len(new.orders):
            logger.info("[state] {} {} orders {} -> {}",
                        new.symbol, new.position.value, len(prev.orders), len(new.orders))

    def _reconcile_orders(self, state: SymbolState) -> None:
        """Apply the orphan-cleanup rules. Caller has already checked grace period."""
        if state.position == Position.NONE:
            if state.orders:
                ids = [o["order_id"] for o in state.orders]
                logger.warning("[state] {} {} orphan order(s) (no position) — cancelling: {}",
                               state.symbol, len(state.orders), ids)
                for o in state.orders:
                    self._cancel_order(state.symbol, o)
            return

        # Position is open — exit orders should be present.
        exit_side = "SELL" if state.position == Position.LONG else "BUY"
        exit_orders = [o for o in state.orders if o["side"] == exit_side]
        if not exit_orders:
            logger.warning("[state] {} {} position has no exit orders — leaving untouched.",
                           state.symbol, state.position.value)

    def _cancel_order(self, symbol: str, order: dict) -> None:
        try:
            if order["is_algo"]:
                algo_orders.cancel_algo_order(self._client, symbol, order["order_id"])
            else:
                orders.cancel_order(self._client, symbol, order["order_id"])
        except Exception as exc:
            logger.warning("[state] {} could not cancel orphan order {}: {}",
                           symbol, order["order_id"], exc)

    # ------------------------------------------------------------------
    # Daily P&L
    # ------------------------------------------------------------------

    def _refresh_daily_pnl(self) -> None:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(start.timestamp() * 1000)

        if today != self._daily_pnl_day:
            with self._lock:
                self._daily_pnl = Decimal("0")
                self._trade_count = 0
                self._daily_pnl_day = today

        total_net = Decimal("0")
        total_count = 0
        for symbol in self._symbols:
            try:
                trades = account.get_futures_recent_trades(self._client, symbol, start_time_ms=start_ms, limit=1000)
                realized = sum((t["realized_pnl"] for t in trades), Decimal("0"))
                commission = sum((t["commission"] for t in trades), Decimal("0"))
                total_net += realized - commission
                total_count += len(trades)
            except Exception as exc:
                logger.warning("[state] could not refresh P&L for {}: {}", symbol, exc)

        with self._lock:
            self._daily_pnl = total_net
            self._trade_count = total_count
