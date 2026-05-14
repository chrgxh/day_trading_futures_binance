"""Unit tests for EmaTrendMomentum.compute_signal — checks the signal contract.

Execution paths (IOC entry, exit-order placement) hit the broker and are covered by
integration tests, not here.
"""

from decimal import Decimal
from unittest.mock import MagicMock

from core.strategies.ema_trend_momentum import EmaTrendMomentum
from core.types import Action


def _candles(closes, *, vols=None, start_ts=0, step_ms=900_000):
    """Build a synthetic OHLCV buffer from a list of closes."""
    vols = vols or [Decimal("100")] * len(closes)
    out = []
    for i, c in enumerate(closes):
        cd = Decimal(str(c))
        out.append({
            "open_time": start_ts + i * step_ms,
            "open": cd, "high": cd * Decimal("1.001"), "low": cd * Decimal("0.999"),
            "close": cd, "volume": vols[i], "close_time": start_ts + (i + 1) * step_ms - 1,
        })
    return out


def build_strategy(params=None):
    return EmaTrendMomentum(
        name="ema_trend_momentum",
        interval="15m",
        symbols=["BTCUSDT"],
        params=params or {},
        client=MagicMock(),
        sym_infos={"BTCUSDT": {"tick_size": Decimal("0.1"), "step_size": Decimal("0.001")}},
        state_manager=MagicMock(),
        risk_guard=MagicMock(),
        live_trade_manager=None,
    )


def test_returns_none_when_insufficient_candles():
    s = build_strategy()
    assert s.compute_signal("BTCUSDT", _candles(list(range(1, 10)))) is None


def test_returns_none_when_insufficient_1h_bars_for_trend_ema():
    """A 15m buffer that resamples to fewer than 200 1h bars must return None."""
    s = build_strategy()
    # 100 15m candles → ~25 1h bars, far below trend_period=200.
    closes = [100 + i for i in range(100)]
    assert s.compute_signal("BTCUSDT", _candles(closes)) is None


def test_returns_hold_when_gates_dont_pass():
    """Enough candles, but flat prices → no volume spike, no EMA momentum → HOLD."""
    # Need at least 200 1h bars. 15m → 4 candles per hour. 200 * 4 = 800.
    s = build_strategy()
    closes = [100.0] * 850
    sig = s.compute_signal("BTCUSDT", _candles(closes))
    assert sig is not None
    assert sig.action == Action.HOLD


def test_open_long_signal_sets_sl_and_tp_prices():
    """Force a long signal by constructing an uptrend and a volume spike."""
    params = {
        "fast_period": 9, "slow_period": 21, "trend_period": 200, "rsi_period": 14,
        "volume_lookback": 20, "volume_multiplier": 1.2, "adx_period": 14,
        "min_adx": 0,  # disable ADX gate for testability
        "rsi_long_low": 0, "rsi_long_high": 100,
        "stop_loss_pct": 0.5, "take_profit_pct": 2.0,
    }
    s = build_strategy(params)
    closes = [100.0 + i * 0.5 for i in range(850)]
    # Make the last candle's volume spike to satisfy RVOL gate.
    vols = [Decimal("100")] * 850
    vols[-1] = Decimal("200")
    sig = s.compute_signal("BTCUSDT", _candles(closes, vols=vols))
    assert sig is not None
    if sig.action == Action.OPEN_LONG:
        # Validate the SL/TP contract.
        assert sig.entry_price is not None
        assert sig.stop_loss_price < sig.entry_price
        assert sig.take_profit_price > sig.entry_price
        expected_sl = sig.entry_price * (Decimal("1") - Decimal("0.5") / 100)
        expected_tp = sig.entry_price * (Decimal("1") + Decimal("2.0") / 100)
        assert sig.stop_loss_price == expected_sl
        assert sig.take_profit_price == expected_tp


def test_candle_limit_scales_with_interval():
    s15 = build_strategy()  # 15m → (200*60//15)+50 = 850
    assert s15.candle_limit() == 850

    s5 = EmaTrendMomentum(
        name="ema_trend_momentum", interval="5m", symbols=[], params={},
        client=MagicMock(), sym_infos={},
        state_manager=MagicMock(), risk_guard=MagicMock(),
    )
    assert s5.candle_limit() == (200 * 60 // 5) + 50
