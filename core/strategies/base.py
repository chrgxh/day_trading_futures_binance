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
from typing import TYPE_CHECKING, Optional

from binance.client import Client
from loguru import logger

from core.types import Signal

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

        if live_trade_manager is not None:
            live_trade_manager.attach(self)
            state_manager.subscribe(live_trade_manager.on_state_update)

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
