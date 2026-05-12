"""Shared primitives — client factory, retry wrapper, and order normalizers."""

import os
import time
import traceback
from datetime import datetime
from decimal import Decimal


class PostOnlyRejected(RuntimeError):
    """Raised when a GTX (post-only) limit order is rejected because it would execute as a taker."""

import resend
import requests.exceptions
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger


def send_crash_email(exc: BaseException) -> str | None:
    """Send a crash notification email via Resend.

    Reads RESEND_API_KEY, CRASH_NOTIFY_EMAIL, and CRASH_NOTIFY_FROM_EMAIL
    from the environment. Logs a warning and returns None if any are missing
    so a misconfigured notifier never masks the original crash.

    Args:
        exc: The exception that caused the crash.

    Returns:
        The Resend email ID on success, or None if skipped or failed.
    """
    api_key = os.getenv("RESEND_API_KEY")
    to_email = os.getenv("CRASH_NOTIFY_EMAIL")
    from_email = os.getenv("CRASH_NOTIFY_FROM_EMAIL")
    if not api_key or not to_email or not from_email:
        logger.warning("Crash email not sent — RESEND_API_KEY, CRASH_NOTIFY_EMAIL, or CRASH_NOTIFY_FROM_EMAIL not set.")
        return None

    resend.api_key = api_key
    tb = traceback.format_exc()
    body = f"<pre>{type(exc).__name__}: {exc}\n\n{tb}</pre>"

    try:
        response = resend.Emails.send({
            "from": from_email,
            "to": [to_email],
            "subject": f"[Bot Crash] {type(exc).__name__}: {exc}",
            "html": body,
        })
        email_id = response["id"]
        logger.info("Crash notification email sent to {}. id={}", to_email, email_id)
        return email_id
    except Exception as mail_exc:
        logger.error("Failed to send crash email: {}", mail_exc)
        return None


def _read_log_warnings_errors(log_file: str, report_date: datetime) -> list[str]:
    """Return WARNING/ERROR/CRITICAL lines from *log_file* that belong to *report_date* (UTC)."""
    date_prefix = report_date.strftime("%Y-%m-%d")
    lines: list[str] = []
    try:
        with open(log_file, "r", errors="replace") as fh:
            for line in fh:
                if date_prefix in line and any(lvl in line for lvl in ("| WARNING", "| ERROR", "| CRITICAL")):
                    lines.append(line.rstrip())
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.error("Daily report: could not read log file {}: {}", log_file, exc)
    return lines


def send_daily_report_email(rows: list[dict], log_file: str, report_date: datetime) -> str | None:
    """Send a daily P&L + warnings/errors email via Resend.

    Reads RESEND_API_KEY, CRASH_NOTIFY_EMAIL, and CRASH_NOTIFY_FROM_EMAIL from
    the environment. Logs a warning and returns None if any are missing.

    Args:
        rows: P&L rows as returned by write_daily_pnl (symbol rows + TOTAL row).
        log_file: Path to the loguru log file to scan for warnings and errors.
        report_date: The UTC date the report covers.

    Returns:
        The Resend email ID on success, or None if skipped or failed.
    """
    api_key = os.getenv("RESEND_API_KEY")
    to_email = os.getenv("CRASH_NOTIFY_EMAIL")
    from_email = os.getenv("CRASH_NOTIFY_FROM_EMAIL")
    if not api_key or not to_email or not from_email:
        logger.warning("Daily report email not sent — RESEND_API_KEY, CRASH_NOTIFY_EMAIL, or CRASH_NOTIFY_FROM_EMAIL not set.")
        return None

    date_str = report_date.strftime("%Y-%m-%d")

    table_rows_html = ""
    for row in rows:
        is_total = row["symbol"] == "TOTAL"
        row_style = " style=\"font-weight:bold;background:#f0f0f0\"" if is_total else ""
        net_color = "green" if float(row["net_pnl"]) >= 0 else "red"
        table_rows_html += (
            f"<tr{row_style}>"
            f"<td>{row['symbol']}</td>"
            f"<td>{row['realized_pnl']}</td>"
            f"<td>{row['commission']}</td>"
            f"<td style=\"color:{net_color}\">{row['net_pnl']}</td>"
            f"<td>{row['trade_count']}</td>"
            f"</tr>"
        )

    table_html = (
        "<table border=\"1\" cellpadding=\"6\" cellspacing=\"0\" style=\"border-collapse:collapse;font-family:monospace\">"
        "<thead style=\"background:#333;color:#fff\">"
        "<tr><th>Symbol</th><th>Realized PNL</th><th>Commission</th><th>Net PNL</th><th>Trades</th></tr>"
        "</thead>"
        f"<tbody>{table_rows_html}</tbody>"
        "</table>"
    )

    log_lines = _read_log_warnings_errors(log_file, report_date)
    if log_lines:
        escaped = "\n".join(log_lines).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        log_section = (
            f"<h2>Warnings &amp; Errors ({len(log_lines)} entries)</h2>"
            f"<pre style=\"background:#fff8e1;padding:10px;font-size:12px\">{escaped}</pre>"
        )
    else:
        log_section = (
            "<h2>Warnings &amp; Errors</h2>"
            "<p style=\"color:green\">No warnings or errors logged for this day.</p>"
        )

    total_row = next((r for r in rows if r["symbol"] == "TOTAL"), None)
    total_net = total_row["net_pnl"] if total_row else "N/A"

    body = (
        "<html><body style=\"font-family:sans-serif;padding:20px\">"
        f"<h1>Daily Trading Report — {date_str}</h1>"
        "<h2>P&amp;L Summary</h2>"
        f"{table_html}"
        f"{log_section}"
        "</body></html>"
    )

    resend.api_key = api_key
    try:
        response = resend.Emails.send({
            "from": from_email,
            "to": [to_email],
            "subject": f"[Bot Report] {date_str} — Net P&L: {total_net} USDT",
            "html": body,
        })
        email_id = response["id"]
        logger.info("Daily report email sent to {}. id={}", to_email, email_id)
        return email_id
    except Exception as mail_exc:
        logger.error("Failed to send daily report email: {}", mail_exc)
        return None


def build_client(api_key: str, api_secret: str, testnet: bool = True) -> Client:
    """Create and return an authenticated Binance client.

    Args:
        api_key: Binance API key.
        api_secret: Binance API secret.
        testnet: If True, connects to the testnet endpoint.

    Returns:
        Authenticated Binance Client instance.
    """
    client = Client(api_key, api_secret, testnet=testnet)
    logger.info("Binance client initialised (testnet={})", testnet)
    return client


def with_retry(fn, retries: int = 3, backoff: float = 2.0):
    """Call fn(), retrying up to `retries` times with exponential backoff.

    Args:
        fn: Zero-argument callable to attempt.
        retries: Maximum number of attempts.
        backoff: Base sleep seconds between attempts (doubles each retry).

    Returns:
        Return value of fn() on success.

    Raises:
        The last exception raised by fn() after all retries are exhausted.
    """
    delay = backoff
    last_exc: Exception = RuntimeError("with_retry called with retries=0")
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except (BinanceAPIException, BinanceRequestException, requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            logger.warning("Attempt {}/{} failed: {}. Retrying in {}s.", attempt, retries, exc, delay)
            time.sleep(delay)
            delay *= 2
    logger.error("All {} retries exhausted.", retries)
    raise last_exc


def round_price(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round a price to the nearest tick boundary.

    Args:
        price: Raw price to round.
        tick_size: Minimum price increment for the symbol.

    Returns:
        Price rounded to the nearest tick (half-up).
    """
    return (price / tick_size).to_integral_value() * tick_size


def _normalize_order(raw: dict) -> dict:
    """Normalize a raw Binance order response to a consistent shape."""
    return {
        "order_id": raw["orderId"],
        "symbol": raw["symbol"],
        "side": raw["side"],
        "type": raw["type"],
        "quantity": Decimal(raw["origQty"]),
        "executed_qty": Decimal(raw.get("executedQty") or "0"),
        "price": Decimal(raw.get("price") or "0"),
        "stop_price": Decimal(raw.get("stopPrice") or "0"),
        "status": raw["status"],
        "time": raw.get("updateTime", raw.get("time", 0)),
        "is_algo": False,
    }


def _normalize_algo_order(raw: dict) -> dict:
    """Normalize a Binance algo order response to the same shape as _normalize_order.

    The creation response is minimal (algoId, code, msg only). Placement functions
    enrich it with the original params before calling this so all fields are present.
    Query responses (get_open_orders) include the full set of fields.
    Algo orders use WORKING instead of NEW for active status.
    """
    return {
        "order_id": raw["algoId"],
        "symbol": raw.get("symbol", ""),
        "side": raw.get("side", ""),
        "type": raw.get("type", ""),
        "quantity": Decimal(raw.get("origQty") or "0"),
        "price": Decimal(raw.get("price") or "0"),
        "stop_price": Decimal(raw.get("triggerPrice") or "0"),
        "status": raw.get("status", "WORKING"),
        "time": raw.get("updateTime", raw.get("bookTime", 0)),
        "is_algo": True,
    }
