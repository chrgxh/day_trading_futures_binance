"""Strategy registry. Add a new strategy class and register it in STRATEGIES."""

from core.strategies.base import Strategy
from core.strategies.ema_trend_momentum import EmaTrendMomentum

STRATEGIES: dict[str, type[Strategy]] = {
    "ema_trend_momentum": EmaTrendMomentum,
}
