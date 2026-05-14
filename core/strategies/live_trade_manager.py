"""LiveTradeManager base — optional per-strategy post-fill lifecycle hooks.

Subscribes to StateManager updates. Strategy-specific subclasses override the hooks
to implement SL migration, partial TP handling, stagnation exits, etc.

The base class provides no behavior — it just routes state updates and tracks which
symbols this strategy currently has open. Concrete behavior is added by subclasses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from core.types import Position, SymbolState

if TYPE_CHECKING:
    from core.strategies.base import Strategy


class LiveTradeManager:
    """Base LiveTradeManager. Override the hooks in subclasses for strategy-specific behavior."""

    def __init__(self, *, params: dict) -> None:
        self.params = params
        self.strategy: "Strategy | None" = None
        self._held: set[str] = set()

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def attach(self, strategy: "Strategy") -> None:
        self.strategy = strategy

    @property
    def tag(self) -> str:
        name = self.strategy.name if self.strategy else "<detached>"
        return f"[{name}:ltm]"

    # ------------------------------------------------------------------
    # Public hooks
    # ------------------------------------------------------------------

    def register_open(self, symbol: str) -> None:
        """Called by the strategy after it successfully opens a position."""
        self._held.add(symbol)
        logger.debug("{} {} registered as held", self.tag, symbol)
        self.on_open(symbol)

    def on_state_update(self, state: SymbolState) -> None:
        """Called by StateManager on every poll for every symbol this strategy trades."""
        if self.strategy is None or state.symbol not in self.strategy.symbols:
            return

        was_held = state.symbol in self._held
        is_held = state.position != Position.NONE

        if was_held and not is_held:
            self._held.discard(state.symbol)
            self.on_close(state.symbol)
            return
        if is_held:
            self.on_update(state)

    # ------------------------------------------------------------------
    # Override in subclasses
    # ------------------------------------------------------------------

    def on_open(self, symbol: str) -> None:
        """Hook fired when the strategy opens a new position. Override for setup."""

    def on_close(self, symbol: str) -> None:
        """Hook fired the first poll where Binance reports the position is gone."""

    def on_update(self, state: SymbolState) -> None:
        """Hook fired on every poll while the position is open. Override for lifecycle logic
        like SL migration, partial-fill re-stop, stagnation, etc."""
