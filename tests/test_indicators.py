"""Unit tests for indicators.py."""

from decimal import Decimal

from utils.indicators import Signal, TradeSignal, moving_average_crossover


def _make_candles(closes: list[float]) -> list[dict]:
    return [
        {
            "open_time": 0,
            "open": Decimal("100.0"),
            "high": Decimal("110.0"),
            "low": Decimal("90.0"),
            "close": Decimal(str(c)),
            "volume": Decimal("500.0"),
            "close_time": 60000,
        }
        for c in closes
    ]


class TestMovingAverageCrossover:
    SYMBOL = "BTCUSDT"

    def test_insufficient_data_returns_hold(self):
        result = moving_average_crossover(_make_candles([100.0] * 5), self.SYMBOL, fast_period=9, slow_period=21)
        assert result.signal == Signal.HOLD
        assert result.symbol == self.SYMBOL

    def test_no_crossover_returns_hold(self):
        result = moving_average_crossover(_make_candles([100.0] * 30), self.SYMBOL)
        assert result.signal == Signal.HOLD

    def test_bullish_crossover_returns_buy(self):
        result = moving_average_crossover(_make_candles([100.0] * 29 + [200.0]), self.SYMBOL)
        assert result.signal == Signal.BUY

    def test_bearish_crossover_returns_sell(self):
        result = moving_average_crossover(_make_candles([100.0] * 29 + [0.0]), self.SYMBOL)
        assert result.signal == Signal.SELL

    def test_return_type_is_trade_signal(self):
        result = moving_average_crossover(_make_candles([100.0] * 30), self.SYMBOL)
        assert isinstance(result, TradeSignal)
        assert result.symbol == self.SYMBOL
        assert result.reason != ""
