"""Shared primitives — client factory, retry wrapper, and order normalizers."""

import os
import time
import traceback
from decimal import Decimal


class PostOnlyRejected(RuntimeError):
    """Raised when a GTX (post-only) limit order is rejected because it would execute as a taker."""

import resend
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
        except (BinanceAPIException, BinanceRequestException) as exc:
            last_exc = exc
            logger.warning("Attempt {}/{} failed: {}. Retrying in {}s.", attempt, retries, exc, delay)
            time.sleep(delay)
            delay *= 2
    logger.error("All {} retries exhausted.", retries)
    raise last_exc


def _normalize_order(raw: dict) -> dict:
    """Normalize a raw Binance order response to a consistent shape."""
    return {
        "order_id": raw["orderId"],
        "symbol": raw["symbol"],
        "side": raw["side"],
        "type": raw["type"],
        "quantity": Decimal(raw["origQty"]),
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
