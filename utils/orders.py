"""Regular order placement and cancellation for Binance Futures."""

from decimal import Decimal

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger

from utils.algo_orders import cancel_algo_order
from utils.general import PostOnlyRejected, _normalize_algo_order, _normalize_order, with_retry


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


def place_limit_order(
    client: Client,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    time_in_force: str = "GTC",
) -> dict:
    """Place a futures limit order.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL".
        quantity: Order quantity in base asset units.
        price: Limit price.
        time_in_force: "GTC" (default) keeps the order open until cancelled.
            "GTX" (Post-Only) is cancelled immediately if it would execute as a
            taker; raises PostOnlyRejected in that case rather than retrying.

    Returns:
        Normalised order dict (see _normalize_order).

    Raises:
        PostOnlyRejected: Only when time_in_force="GTX" and the order would
            fill immediately as a taker.
    """
    try:
        if time_in_force == "GTX":
            # Post-only: do not retry on rejection — it is deterministic, not transient.
            raw = client.futures_create_order(
                symbol=symbol, side=side, type="LIMIT",
                timeInForce="GTX", quantity=str(quantity), price=str(price),
            )
        else:
            raw = with_retry(lambda: client.futures_create_order(
                symbol=symbol, side=side, type="LIMIT",
                timeInForce=time_in_force, quantity=str(quantity), price=str(price),
            ))
        order = _normalize_order(raw)
        logger.info(
            "Limit order placed: {} {} {} @ {} tif={} | id={} status={}",
            side, quantity, symbol, price, time_in_force, order["order_id"], order["status"],
        )
        return order
    except BinanceAPIException as exc:
        if time_in_force == "GTX" and (
            "GTX" in str(exc) or getattr(exc, "code", None) == -4129
        ):
            logger.info("GTX post-only order rejected (would be taker): {} {} {} @ {}", side, quantity, symbol, price)
            raise PostOnlyRejected(str(exc)) from exc
        logger.error("place_limit_order failed ({} {} {} @ {} tif={}): {}", side, quantity, symbol, price, time_in_force, exc)
        raise
    except BinanceRequestException as exc:
        logger.error("place_limit_order failed ({} {} {} @ {} tif={}): {}", side, quantity, symbol, price, time_in_force, exc)
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


def place_trailing_stop_order(
    client: Client,
    symbol: str,
    side: str,
    quantity: Decimal,
    callback_rate: Decimal,
    activation_price: Decimal | None = None,
) -> dict:
    """Place a trailing-stop-market order that follows price and triggers on reversal.

    The stop price trails the best mark price seen since activation by callback_rate%.
    For a long (side=SELL): stop trails upward and triggers when price falls back by
    callback_rate%. For a short (side=BUY): stop trails downward and triggers on reversal.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL" — opposite to the open position side.
        quantity: Order quantity in base asset units.
        callback_rate: Trailing callback percentage (0.1–5.0).
        activation_price: Mark price at which trailing begins. If None, starts immediately
            from the best price seen at order placement.

    Returns:
        Normalised order dict (is_algo=True). Cancel via cancel_algo_order(), not cancel_order().
        Binance treats TRAILING_STOP_MARKET as a conditional order and returns an algoId.
    """
    try:
        params: dict = dict(
            symbol=symbol,
            side=side,
            type="TRAILING_STOP_MARKET",
            quantity=str(quantity),
            callbackRate=str(callback_rate),
            reduceOnly=True,
        )
        if activation_price is not None:
            params["activationPrice"] = str(activation_price)
        raw = with_retry(lambda: client.futures_create_order(**params))
        order = _normalize_algo_order({**params, "origQty": params["quantity"], **raw})
        logger.info(
            "Trailing stop order placed: {} {} {} callback={}% activation={} | id={} status={}",
            side, quantity, symbol, callback_rate, activation_price, order["order_id"], order["status"],
        )
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error(
            "place_trailing_stop_order failed ({} {} {} callback={}%): {}",
            side, quantity, symbol, callback_rate, exc,
        )
        raise


def get_order(client: Client, symbol: str, order_id: int) -> dict:
    """Fetch the current status of a specific order.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        order_id: The order ID to query.

    Returns:
        Normalised order dict (see _normalize_order).
    """
    try:
        raw = with_retry(lambda: client.futures_get_order(symbol=symbol, orderId=order_id))
        order = _normalize_order(raw)
        logger.debug("Order {} status: {} for {}", order_id, order["status"], symbol)
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_order failed (id={} {}): {}", order_id, symbol, exc)
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
