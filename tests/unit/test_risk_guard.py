"""Unit tests for RiskGuard."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from core.risk_guard import RiskGuard


def make_sm(has_position=False, open_count=0, pnl: Decimal = Decimal("0")):
    sm = MagicMock()
    sm.has_position.return_value = has_position
    sm.open_position_count.return_value = open_count
    sm.daily_pnl.return_value = pnl
    return sm


def make_strategy():
    s = MagicMock()
    s.tag = "[test]"
    return s


def test_blocks_when_symbol_already_held():
    sm = make_sm(has_position=True)
    rg = RiskGuard(state_manager=sm, max_concurrent_positions=10, max_daily_loss_usdt=100)
    assert rg.allow_open("BTCUSDT", make_strategy()) is False


def test_blocks_at_max_concurrent_positions():
    sm = make_sm(open_count=3)
    rg = RiskGuard(state_manager=sm, max_concurrent_positions=3, max_daily_loss_usdt=100)
    assert rg.allow_open("BTCUSDT", make_strategy()) is False


def test_allows_when_below_limits():
    sm = make_sm(open_count=1)
    rg = RiskGuard(state_manager=sm, max_concurrent_positions=3, max_daily_loss_usdt=100)
    assert rg.allow_open("BTCUSDT", make_strategy()) is True


def test_blocks_when_daily_loss_exceeds_limit():
    sm = make_sm(pnl=Decimal("-150"))
    rg = RiskGuard(state_manager=sm, max_concurrent_positions=3, max_daily_loss_usdt=100)
    assert rg.allow_open("BTCUSDT", make_strategy()) is False


def test_daily_loss_latch_persists_within_same_day(monkeypatch):
    sm = make_sm(pnl=Decimal("-150"))
    rg = RiskGuard(state_manager=sm, max_concurrent_positions=3, max_daily_loss_usdt=100)
    # Silence email
    monkeypatch.setattr("core.risk_guard.general.send_crash_email", lambda exc: None)
    assert rg.allow_open("BTCUSDT", make_strategy()) is False
    # Even if PnL recovers within the same UTC day, still blocked.
    sm.daily_pnl.return_value = Decimal("0")
    assert rg.allow_open("BTCUSDT", make_strategy()) is False


def test_daily_loss_latch_resets_next_utc_day(monkeypatch):
    sm = make_sm(pnl=Decimal("-150"))
    rg = RiskGuard(state_manager=sm, max_concurrent_positions=3, max_daily_loss_usdt=100)
    monkeypatch.setattr("core.risk_guard.general.send_crash_email", lambda exc: None)

    fake_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return fake_now

    monkeypatch.setattr("core.risk_guard.datetime", FakeDT)
    assert rg.allow_open("BTCUSDT", make_strategy()) is False  # trip on day 1

    # Advance to next UTC day with PnL recovered.
    fake_now = datetime(2026, 1, 2, 0, 1, 0, tzinfo=timezone.utc)
    sm.daily_pnl.return_value = Decimal("0")
    assert rg.allow_open("BTCUSDT", make_strategy()) is True
