"""Regular order placement and cancellation for Binance Futures."""

from decimal import Decimal

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger

from utils.algo_orders import cancel_algo_order
from utils.general import _normalize_algo_order, _normalize_order, with_retry


def place_market_order(client: Client, symbol: str, side: str, quantity: Decimal) -> dict:
    """Place a futures market order.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL".
        quantity: Order quantity in base asset units.

    Returns:
        Normalised order dict (see _normalize_order).
    """
    try:
        raw = with_retry(lambda: client.futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=str(quantity),
        ))
        order = _normalize_order(raw)
        logger.info("Market order placed: {} {} {} | id={} status={}", side, quantity, symbol, order["order_id"], order["status"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_market_order failed ({} {} {}): {}", side, quantity, symbol, exc)
        raise


def place_limit_order(client: Client, symbol: str, side: str, quantity: Decimal, price: Decimal) -> dict:
    """Place a futures limit order (GTC).

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL".
        quantity: Order quantity in base asset units.
        price: Limit price.

    Returns:
        Normalised order dict (see _normalize_order).
    """
    try:
        raw = with_retry(lambda: client.futures_create_order(
            symbol=symbol,
            side=side,
            type="LIMIT",
            timeInForce="GTC",
            quantity=str(quantity),
            price=str(price),
        ))
        order = _normalize_order(raw)
        logger.info("Limit order placed: {} {} {} @ {} | id={} status={}", side, quantity, symbol, price, order["order_id"], order["status"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_limit_order failed ({} {} {} @ {}): {}", side, quantity, symbol, price, exc)
        raise


def get_open_orders(client: Client, symbol: str) -> list[dict]:
    """Return all open orders for a symbol, including conditional algo orders.

    Fetches from both /fapi/v1/openOrders (regular) and /fapi/v1/openAlgoOrders
    (conditional), since conditional orders no longer appear in the regular endpoint
    after Binance's 2025-12-09 migration.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".

    Returns:
        List of normalised order dicts (is_algo=False for regular, True for algo orders).
    """
    try:
        regular = with_retry(lambda: client.futures_get_open_orders(symbol=symbol))
        algo_resp = with_retry(lambda: client.futures_get_open_algo_orders(symbol=symbol))
        algo = algo_resp.get("orders", []) if isinstance(algo_resp, dict) else algo_resp
        result = [_normalize_order(o) for o in regular] + [_normalize_algo_order(o) for o in algo]
        logger.info("Open orders for {} (regular={} algo={})", symbol, len(regular), len(algo))
        return result
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_open_orders failed for {}: {}", symbol, exc)
        raise


def cancel_order(client: Client, symbol: str, order_id: int) -> dict:
    """Cancel a specific regular (non-algo) open order.

    For cancelling conditional orders use algo_orders.cancel_algo_order instead.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        order_id: The order ID to cancel.

    Returns:
        Normalised order dict of the cancelled order.
    """
    try:
        raw = with_retry(lambda: client.futures_cancel_order(symbol=symbol, orderId=order_id))
        order = _normalize_order(raw)
        logger.info("Order {} cancelled for {}", order_id, symbol)
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("cancel_order failed (id={} {}): {}", order_id, symbol, exc)
        raise


def cancel_all_orders(client: Client, symbol: str) -> None:
    """Cancel all open orders for a symbol, including conditional algo orders.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
    """
    try:
        with_retry(lambda: client.futures_cancel_all_open_orders(symbol=symbol))
        algo_resp = with_retry(lambda: client.futures_get_open_algo_orders(symbol=symbol))
        algo_orders = algo_resp.get("orders", []) if isinstance(algo_resp, dict) else algo_resp
        for o in algo_orders:
            cancel_algo_order(client, symbol, o["algoId"])
        logger.info("All open orders cancelled for {} (algo={})", symbol, len(algo_orders))
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("cancel_all_orders failed for {}: {}", symbol, exc)
        raise
