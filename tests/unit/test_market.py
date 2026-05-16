"""Unit tests for utils/market.py — pure candle helpers (no network)."""

from datetime import datetime, timezone
from decimal import Decimal

from utils.market import drop_forming_candle

_HOUR_MS = 60 * 60 * 1000


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _candle(open_time: int, close_time: int) -> dict:
    """Minimal OHLCV candle dict — drop_forming_candle only reads close_time."""
    return {
        "open_time": open_time,
        "open": Decimal("1"),
        "high": Decimal("1"),
        "low": Decimal("1"),
        "close": Decimal("1"),
        "volume": Decimal("1"),
        "close_time": close_time,
    }


def test_drop_forming_candle_empty_list():
    assert drop_forming_candle([]) == []


def test_drop_forming_candle_removes_trailing_forming_candle():
    now = _now_ms()
    closed = _candle(now - 8 * _HOUR_MS, now - 4 * _HOUR_MS - 1)
    forming = _candle(now - 4 * _HOUR_MS, now + 4 * _HOUR_MS - 1)  # close_time in future
    result = drop_forming_candle([closed, forming])
    assert result == [closed]


def test_drop_forming_candle_keeps_all_closed_candles():
    now = _now_ms()
    c1 = _candle(now - 8 * _HOUR_MS, now - 4 * _HOUR_MS - 1)
    c2 = _candle(now - 4 * _HOUR_MS, now - 1)  # closed: close_time just in the past
    candles = [c1, c2]
    result = drop_forming_candle(candles)
    assert result == candles


def test_drop_forming_candle_only_a_forming_candle():
    now = _now_ms()
    forming = _candle(now, now + _HOUR_MS)
    assert drop_forming_candle([forming]) == []
