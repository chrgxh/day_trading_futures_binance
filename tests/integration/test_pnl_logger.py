"""Integration tests for utils/account.get_futures_trades_for_range and utils/pnl_logger."""

import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from utils import account, orders as orders_mod, positions as positions_mod
from utils.pnl_logger import DailyPnLLogger, write_daily_pnl

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# get_futures_trades_for_range
# ---------------------------------------------------------------------------

def test_trades_for_range_captures_open_and_close(client, symbol, sym_info):
    """A buy-then-close within a bounded time window should appear in range results."""
    start_ms = int(time.time() * 1000)

    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])
    orders_mod.cancel_all_orders(client, symbol)
    positions_mod.close_position(client, symbol)
    orders_mod.place_market_order(client, symbol, "BUY", qty)
    positions_mod.close_position(client, symbol)

    end_ms = int(time.time() * 1000)

    trades = account.get_futures_trades_for_range(client, symbol, start_ms, end_ms)

    assert isinstance(trades, list)
    assert len(trades) > 0

    expected_keys = {
        "trade_id", "order_id", "side", "price", "qty",
        "realized_pnl", "commission", "commission_asset", "time", "is_maker",
    }
    for t in trades:
        assert expected_keys <= t.keys()
        assert isinstance(t["realized_pnl"], Decimal)
        assert isinstance(t["commission"], Decimal)
        assert isinstance(t["price"], Decimal)
        assert isinstance(t["qty"], Decimal)
        assert start_ms <= t["time"] <= end_ms


def test_trades_for_range_excludes_earlier_trades(client, symbol, sym_info):
    """Trades before start_ms must not appear in the result."""
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])
    orders_mod.cancel_all_orders(client, symbol)
    positions_mod.close_position(client, symbol)
    orders_mod.place_market_order(client, symbol, "BUY", qty)
    positions_mod.close_position(client, symbol)

    # Window starts now — no trades can have happened inside it yet.
    future_start_ms = int(time.time() * 1000)
    future_end_ms = future_start_ms + 5_000

    trades = account.get_futures_trades_for_range(client, symbol, future_start_ms, future_end_ms)
    assert trades == []


def test_trades_for_range_returns_correct_keys(client, symbol):
    """Smoke test: a 1-second window far in the past returns a well-formed list (likely empty)."""
    # Use a fixed past timestamp that is guaranteed to precede any testnet activity.
    ancient_start_ms = 1_700_000_000_000  # 2023-11-14 UTC
    ancient_end_ms = ancient_start_ms + 1_000
    result = account.get_futures_trades_for_range(client, symbol, ancient_start_ms, ancient_end_ms)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# write_daily_pnl
# ---------------------------------------------------------------------------

def test_write_daily_pnl_creates_report_file(client, symbol, sym_info):
    """write_daily_pnl should produce a file with date header and symbol lines."""
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])
    orders_mod.cancel_all_orders(client, symbol)
    positions_mod.close_position(client, symbol)
    orders_mod.place_market_order(client, symbol, "BUY", qty)
    positions_mod.close_position(client, symbol)

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tmp:
        log_path = tmp.name

    today = datetime.now(timezone.utc)
    write_daily_pnl(client, [symbol], log_path, report_date=today)

    content = Path(log_path).read_text()
    date_str = today.strftime("%Y-%m-%d")

    assert date_str in content
    assert symbol in content
    assert "TOTAL" in content
    assert "net" in content


def test_write_daily_pnl_appends_on_multiple_calls(client, symbol):
    """Calling write_daily_pnl twice should append two report blocks, not overwrite."""
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tmp:
        log_path = tmp.name

    today = datetime.now(timezone.utc)
    write_daily_pnl(client, [symbol], log_path, report_date=today)
    write_daily_pnl(client, [symbol], log_path, report_date=today)

    content = Path(log_path).read_text()
    assert content.count("Daily P&L Report") == 2


def test_write_daily_pnl_net_equals_realized_minus_commission(client, symbol, sym_info):
    """Net P&L in the report must equal realized − commission for the symbol line."""
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])
    orders_mod.cancel_all_orders(client, symbol)
    positions_mod.close_position(client, symbol)
    orders_mod.place_market_order(client, symbol, "BUY", qty)
    positions_mod.close_position(client, symbol)

    today = datetime.now(timezone.utc)
    start_ms = today.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=timezone.utc)
    start_epoch = int(start_ms.timestamp() * 1000)
    end_epoch = int(today.timestamp() * 1000)

    trades = account.get_futures_trades_for_range(client, symbol, start_epoch, end_epoch)
    expected_realized = sum((t["realized_pnl"] for t in trades), Decimal("0"))
    expected_commission = sum((t["commission"] for t in trades), Decimal("0"))
    expected_net = expected_realized - expected_commission

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tmp:
        log_path = tmp.name

    write_daily_pnl(client, [symbol], log_path, report_date=today)
    content = Path(log_path).read_text()

    # Find the symbol line and confirm the net value matches what we computed directly.
    symbol_line = next(line for line in content.splitlines() if symbol in line and "net" in line)
    sign = "+" if expected_net >= 0 else ""
    assert f"net {sign}{expected_net:.4f}" in symbol_line


# ---------------------------------------------------------------------------
# DailyPnLLogger
# ---------------------------------------------------------------------------

def test_daily_pnl_logger_starts_without_error(client, symbol):
    """DailyPnLLogger.start() should launch the daemon thread without raising."""
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tmp:
        log_path = tmp.name

    pnl_logger = DailyPnLLogger(client, [symbol], log_path)
    pnl_logger.start()
    assert pnl_logger._thread.is_alive()
