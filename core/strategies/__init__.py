"""Strategy registry. Add a new strategy class and register it in STRATEGIES."""

from core.strategies.adaptive_trend_pullback import AdaptiveTrendPullback
from core.strategies.base import Strategy
from core.strategies.bb_rsi_mean_reversion import BBRsiMeanReversion

STRATEGIES: dict[str, type[Strategy]] = {
    "adaptive_trend_pullback": AdaptiveTrendPullback,
    "bb_rsi_mean_reversion": BBRsiMeanReversion,
}
