"""Strategy ABC.

A Strategy owns:
- a candle buffer per symbol (its private state)
- the signal computation (compute_signal)
- the execution mechanics (execute_open / execute_close) — IOC, market, layered limits, etc.
- an optional LiveTradeManager for post-fill lifecycle

The bot is dumb routing only. It calls strategy.on_candle(symbol, candle); the strategy
is responsible for everything else, including consulting the StateManager to check whether
the symbol is held (one-position-per-symbol rule is enforced via RiskGuard before any entry).
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
    """Per-strategy class. One instance handles all symbols at one interval.

    Subclasses implement compute_signal(symbol) -> Signal and the open/close execution.
    """

    def __init__(
        self,
        *,
        name: str,
        interval: str,
        symbols: list[str],
        params: dict,
        client: Client,
        sym_infos: dict[str, dict],
        state_manager: "StateManager",
        risk_guard: "RiskGuard",
        live_trade_manager: Optional["LiveTradeManager"] = None,
    ) -> None:
        self.name = name
        self.interval = interval
        self.symbols = symbols
        self.params = params
        self.client = client
        self.sym_infos = sym_infos
        self.state_manager = state_manager
        self.risk_guard = risk_guard
        self.live_trade_manager = live_trade_manager
        self._buffers: dict[str, list[dict]] = {s: [] for s in symbols}

        if live_trade_manager is not None:
            live_trade_manager.attach(self)
            state_manager.subscribe(live_trade_manager.on_state_update)

    @property
    def tag(self) -> str:
        return f"[{self.name}]"

    def candle_limit(self) -> int:
        """Number of warmup candles this strategy needs per symbol. Override if more is needed."""
        return 250

    def warmup(self, symbol: str, candles: list[dict]) -> None:
        """Seed the per-symbol candle buffer with REST history."""
        self._buffers[symbol] = list(candles)
        logger.info("{} {} warmup: {} candles", self.tag, symbol, len(candles))

    def on_candle(self, symbol: str, candle: dict) -> None:
        """Append the closed candle, check eligibility, run the strategy."""
        self._append_candle(symbol, candle)
        try:
            self._tick(symbol)
        except Exception as exc:
            logger.exception("{} {} tick error: {}", self.tag, symbol, exc)

    def _append_candle(self, symbol: str, candle: dict) -> None:
        buf = self._buffers[symbol]
        if buf and candle["open_time"] == buf[-1]["open_time"]:
            buf[-1] = candle
        else:
            buf.append(candle)
            limit = self.candle_limit()
            if len(buf) > limit:
                del buf[: len(buf) - limit]

    def _tick(self, symbol: str) -> None:
        """One tick: skip if symbol is held by anything, else compute and act."""
        # If any position exists on this symbol — opened by us, another strategy,
        # or pre-existing on restart — this strategy stays silent. One-per-symbol absolute.
        if self.state_manager.has_position(symbol):
            return

        signal = self.compute_signal(symbol, self._buffers[symbol])
        if signal is None:
            return
        logger.info("{} {} {} — {}", self.tag, symbol, signal.action.value, signal.reason)

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
