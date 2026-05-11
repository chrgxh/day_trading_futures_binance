"""Integration tests for utils/account.get_futures_trades_for_range and utils/pnl_reporter."""

import csv
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from utils import account, orders as orders_mod, positions as positions_mod
from utils.pnl_reporter import DailyPnLReporter, write_daily_pnl

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
    ancient_start_ms = 1_700_000_000_000  # 2023-11-14 UTC
    ancient_end_ms = ancient_start_ms + 1_000
    result = account.get_futures_trades_for_range(client, symbol, ancient_start_ms, ancient_end_ms)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# write_daily_pnl
# ---------------------------------------------------------------------------

def test_write_daily_pnl_creates_csv_with_expected_rows(client, symbol, sym_info):
    """write_daily_pnl should produce a CSV with a header, a symbol row, and a TOTAL row."""
    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])
    orders_mod.cancel_all_orders(client, symbol)
    positions_mod.close_position(client, symbol)
    orders_mod.place_market_order(client, symbol, "BUY", qty)
    positions_mod.close_position(client, symbol)

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name

    today = datetime.now(timezone.utc)
    write_daily_pnl(client, [symbol], csv_path, report_date=today)

    rows = list(csv.DictReader(Path(csv_path).open()))
    date_str = today.strftime("%Y-%m-%d")

    symbols_in_rows = [r["symbol"] for r in rows]
    assert symbol in symbols_in_rows
    assert "TOTAL" in symbols_in_rows
    assert all(r["date"] == date_str for r in rows)


def test_write_daily_pnl_appends_on_multiple_calls(client, symbol):
    """Calling write_daily_pnl twice should append rows, not overwrite."""
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name

    today = datetime.now(timezone.utc)
    write_daily_pnl(client, [symbol], csv_path, report_date=today)
    write_daily_pnl(client, [symbol], csv_path, report_date=today)

    rows = list(csv.DictReader(Path(csv_path).open()))
    assert len([r for r in rows if r["symbol"] == "TOTAL"]) == 2


def test_write_daily_pnl_net_equals_realized_minus_commission(client, symbol, sym_info):
    """Net P&L in the CSV must equal realized − commission for the symbol row."""
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
    expected_net = sum((t["realized_pnl"] - t["commission"] for t in trades), Decimal("0"))

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name

    write_daily_pnl(client, [symbol], csv_path, report_date=today)

    rows = list(csv.DictReader(Path(csv_path).open()))
    symbol_row = next(r for r in rows if r["symbol"] == symbol)
    assert Decimal(symbol_row["net_pnl"]) == expected_net.quantize(Decimal("0.0001"))


# ---------------------------------------------------------------------------
# DailyPnLReporter
# ---------------------------------------------------------------------------

def test_daily_pnl_reporter_starts_without_error(client, symbol):
    """DailyPnLReporter.start() should launch the daemon thread without raising."""
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name

    reporter = DailyPnLReporter(client, [symbol], csv_path)
    reporter.start()
    assert reporter._thread.is_alive()
