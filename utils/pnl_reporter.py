"""Daily P&L reporter — writes end-of-day realized P&L to a CSV file."""

import csv
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from binance.client import Client
from loguru import logger

from utils.account import get_futures_trades_for_range

_FIELDNAMES = ["date", "symbol", "realized_pnl", "commission", "net_pnl", "trade_count"]


def _day_bounds_ms(date: datetime) -> tuple[int, int]:
    """Return (start_ms, end_ms) for the UTC calendar day of *date*."""
    start = date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = date.replace(hour=23, minute=59, second=59, microsecond=999000, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def write_daily_pnl(client: Client, symbols: list[str], csv_file: str, report_date: datetime | None = None) -> None:
    """Fetch realized P&L for all symbols for one UTC day and append rows to *csv_file*.

    Net P&L = realized_pnl − commission. Symbols that error are skipped in the
    CSV (the error is logged via loguru). A TOTAL row is always written.

    Args:
        client: Authenticated Binance client.
        symbols: Symbols to report on (should match config trading.symbols).
        csv_file: Path to the P&L CSV file. Appended to, not overwritten.
        report_date: UTC date to cover. Defaults to yesterday (since this is
            called just after midnight to close out the completed day).
    """
    if report_date is None:
        report_date = datetime.now(timezone.utc) - timedelta(days=1)

    date_str = report_date.strftime("%Y-%m-%d")
    start_ms, end_ms = _day_bounds_ms(report_date)

    rows: list[dict] = []
    total_realized = Decimal("0")
    total_commission = Decimal("0")
    total_trades = 0

    for symbol in symbols:
        try:
            trades = get_futures_trades_for_range(client, symbol, start_ms, end_ms)
            realized = sum((t["realized_pnl"] for t in trades), Decimal("0"))
            commission = sum((t["commission"] for t in trades), Decimal("0"))
            net = realized - commission
            rows.append({
                "date": date_str,
                "symbol": symbol,
                "realized_pnl": f"{realized:.4f}",
                "commission": f"{commission:.4f}",
                "net_pnl": f"{net:.4f}",
                "trade_count": len(trades),
            })
            total_realized += realized
            total_commission += commission
            total_trades += len(trades)
        except Exception as exc:
            logger.error("P&L reporter: failed to fetch {} trades for {}: {}", date_str, symbol, exc)

    total_net = total_realized - total_commission
    rows.append({
        "date": date_str,
        "symbol": "TOTAL",
        "realized_pnl": f"{total_realized:.4f}",
        "commission": f"{total_commission:.4f}",
        "net_pnl": f"{total_net:.4f}",
        "trade_count": total_trades,
    })

    logger.info("Daily P&L {}: net={:.4f} USDT (realized={:.4f}, commission={:.4f})",
                date_str, total_net, total_realized, total_commission)

    try:
        write_header = not os.path.exists(csv_file) or os.path.getsize(csv_file) == 0
        with open(csv_file, "a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)
    except Exception as exc:
        logger.error("P&L reporter: failed to write to {}: {}", csv_file, exc)


class DailyPnLReporter:
    """Background daemon that appends a P&L CSV report just after UTC midnight each day."""

    def __init__(self, client: Client, symbols: list[str], csv_file: str) -> None:
        self._client = client
        self._symbols = symbols
        self._csv_file = csv_file
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="pnl-reporter"
        )

    def start(self) -> None:
        """Start the background reporting thread."""
        self._thread.start()
        logger.info("Daily P&L reporter started — report file: {}", self._csv_file)

    def _seconds_until_next_midnight(self) -> float:
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0
        )
        return (next_midnight - now).total_seconds()

    def _run(self) -> None:
        while True:
            wait = self._seconds_until_next_midnight()
            logger.debug("P&L reporter: sleeping {:.0f}s until next report", wait)
            time.sleep(wait)
            try:
                write_daily_pnl(self._client, self._symbols, self._csv_file)
            except Exception as exc:
                logger.error("P&L reporter: unhandled error during report: {}", exc)
