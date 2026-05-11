"""Daily P&L reporter — writes end-of-day realized P&L summaries to a dedicated log file."""

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from binance.client import Client
from loguru import logger

from utils.account import get_futures_trades_for_range


def _day_bounds_ms(date: datetime) -> tuple[int, int]:
    """Return (start_ms, end_ms) for the UTC calendar day of *date*."""
    start = date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = date.replace(hour=23, minute=59, second=59, microsecond=999000, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def write_daily_pnl(client: Client, symbols: list[str], log_file: str, report_date: datetime | None = None) -> None:
    """Fetch realized P&L for all symbols for one UTC day and append a report to *log_file*.

    Net P&L = realized_pnl − commission. Entries with no trades are included
    so the report is always complete for every configured symbol.

    Args:
        client: Authenticated Binance client.
        symbols: Symbols to report on (should match config trading.symbols).
        log_file: Path to the PnL log file. Appended to, not overwritten.
        report_date: UTC date to cover. Defaults to yesterday (since this is
            called just after midnight to close out the completed day).
    """
    if report_date is None:
        report_date = datetime.now(timezone.utc) - timedelta(days=1)

    date_str = report_date.strftime("%Y-%m-%d")
    start_ms, end_ms = _day_bounds_ms(report_date)

    per_symbol: dict[str, dict] = {}
    total_realized = Decimal("0")
    total_commission = Decimal("0")

    for symbol in symbols:
        try:
            trades = get_futures_trades_for_range(client, symbol, start_ms, end_ms)
            realized = sum((t["realized_pnl"] for t in trades), Decimal("0"))
            commission = sum((t["commission"] for t in trades), Decimal("0"))
            per_symbol[symbol] = {
                "realized_pnl": realized,
                "commission": commission,
                "net_pnl": realized - commission,
                "trade_count": len(trades),
            }
            total_realized += realized
            total_commission += commission
        except Exception as exc:
            logger.error("PnL logger: failed to fetch {} trades for {}: {}", date_str, symbol, exc)
            per_symbol[symbol] = {"error": str(exc)}

    total_net = total_realized - total_commission

    lines = [
        "",
        "=" * 62,
        f"  Daily P&L Report  —  {date_str}  (UTC)",
        "=" * 62,
    ]
    for symbol, data in per_symbol.items():
        if "error" in data:
            lines.append(f"  {symbol:<12}  ERROR: {data['error']}")
        else:
            sign = "+" if data["net_pnl"] >= 0 else ""
            lines.append(
                f"  {symbol:<12}  net {sign}{data['net_pnl']:.4f} USDT"
                f"  (realized {data['realized_pnl']:+.4f},"
                f" commission -{data['commission']:.4f},"
                f" {data['trade_count']} trade(s))"
            )

    lines.append("─" * 62)
    total_sign = "+" if total_net >= 0 else ""
    lines.append(
        f"  {'TOTAL':<12}  net {total_sign}{total_net:.4f} USDT"
        f"  (realized {total_realized:+.4f}, commission -{total_commission:.4f})"
    )
    lines.append("=" * 62)
    lines.append("")

    report = "\n".join(lines)
    logger.info("Daily P&L {}: net={:.4f} USDT (realized={:.4f}, commission={:.4f})",
                date_str, total_net, total_realized, total_commission)

    try:
        with open(log_file, "a") as fh:
            fh.write(report)
    except Exception as exc:
        logger.error("PnL logger: failed to write report to {}: {}", log_file, exc)


class DailyPnLLogger:
    """Background daemon that writes a P&L report just after UTC midnight each day."""

    def __init__(self, client: Client, symbols: list[str], log_file: str) -> None:
        self._client = client
        self._symbols = symbols
        self._log_file = log_file
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="pnl-logger"
        )

    def start(self) -> None:
        """Start the background reporting thread."""
        self._thread.start()
        logger.info("Daily P&L logger started — report file: {}", self._log_file)

    def _seconds_until_next_midnight(self) -> float:
        now = datetime.now(timezone.utc)
        # 5 seconds past midnight so the day boundary is firmly crossed
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0
        )
        return (next_midnight - now).total_seconds()

    def _run(self) -> None:
        while True:
            wait = self._seconds_until_next_midnight()
            logger.debug("PnL logger: sleeping {:.0f}s until next report", wait)
            time.sleep(wait)
            try:
                write_daily_pnl(self._client, self._symbols, self._log_file)
            except Exception as exc:
                logger.error("PnL logger: unhandled error during report: {}", exc)
