"""Unit tests for strategies.py. utils.py is mocked — no live API calls."""

from decimal import Decimal

import pytest

from strategies import Signal, TradeSignal, moving_average_crossover


def _make_klines(closes: list[float]) -> list[list]:
    """Build minimal fake kline rows where index 4 is the close price."""
    return [
        [0, "0", "0", "0", str(c), "0", 0, "0", 0, "0", "0", "0"]
        for c in closes
    ]


# ---------------------------------------------------------------------------
# moving_average_crossover
# ---------------------------------------------------------------------------

class TestMovingAverageCrossover:
    SYMBOL = "BTCUSDT"

    def test_insufficient_data_returns_hold(self):
        klines = _make_klines([100.0] * 5)
        result = moving_average_crossover(klines, self.SYMBOL, fast_period=9, slow_period=21)
        assert result.signal == Signal.HOLD
        assert result.symbol == self.SYMBOL

    def test_no_crossover_returns_hold(self):
        # Flat prices — fast and slow MA are identical, no crossover
        closes = [100.0] * 30
        klines = _make_klines(closes)
        result = moving_average_crossover(klines, self.SYMBOL)
        assert result.signal == Signal.HOLD

    def test_bullish_crossover_returns_buy(self):
        # 29 flat candles keep fast==slow, then one large spike causes fast to
        # cross above slow on exactly the last bar.
        closes = [100.0] * 29 + [200.0]
        klines = _make_klines(closes)
        result = moving_average_crossover(klines, self.SYMBOL)
        assert result.signal == Signal.BUY

    def test_bearish_crossover_returns_sell(self):
        # 29 flat candles keep fast==slow, then a crash to 0 causes fast to
        # cross below slow on exactly the last bar.
        closes = [100.0] * 29 + [0.0]
        klines = _make_klines(closes)
        result = moving_average_crossover(klines, self.SYMBOL)
        assert result.signal == Signal.SELL

    def test_return_type_is_trade_signal(self):
        closes = [100.0] * 30
        klines = _make_klines(closes)
        result = moving_average_crossover(klines, self.SYMBOL)
        assert isinstance(result, TradeSignal)
        assert result.symbol == self.SYMBOL
        assert result.reason != ""
