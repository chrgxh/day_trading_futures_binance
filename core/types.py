"""Shared types used across core modules."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


class Position(Enum):
    NONE = "NONE"
    LONG = "LONG"
    SHORT = "SHORT"


class Action(Enum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


@dataclass
class Signal:
    """A strategy's decision for one symbol on one candle.

    For OPEN_LONG / OPEN_SHORT, the strategy MUST set entry_price, stop_loss_price,
    and take_profit_price — the strategy is the only thing that knows how to size
    the trade. For CLOSE / HOLD only `action` and `reason` are required.
    """

    action: Action
    symbol: str
    reason: str = ""
    entry_price: Optional[Decimal] = None
    stop_loss_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None


@dataclass
class SymbolState:
    """Snapshot of a symbol's live state on Binance, refreshed by StateManager."""

    symbol: str
    position: Position
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    orders: list[dict]  # all open + algo orders for this symbol (utils.orders shape)
