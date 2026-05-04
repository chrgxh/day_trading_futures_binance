"""Conditional (algo) order placement and cancellation for Binance Futures.

All functions here use the Algo Order API (POST /fapi/v1/algoOrder), required since
Binance migrated all conditional order types off /fapi/v1/order on 2025-12-09.
"""

from decimal import Decimal

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger

from utils.general import _normalize_algo_order, with_retry


def place_stop_market_order(
    client: Client,
    symbol: str,
    side: str,
    quantity: Decimal,
    stop_price: Decimal,
) -> dict:
    """Place a stop-market order (triggers a market fill when stop_price is hit).

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL" — typically opposite to the open position side.
        quantity: Order quantity in base asset units.
        stop_price: Price that triggers the order.

    Returns:
        Normalised order dict. status will be "WORKING".
    """
    try:
        params: dict = dict(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            algoType="CONDITIONAL",
            quantity=str(quantity),
            triggerPrice=str(stop_price),
            reduceOnly=True,
        )
        raw = with_retry(lambda: client.futures_create_algo_order(**params))
        order = _normalize_algo_order({**params, "origQty": params["quantity"], **raw})
        logger.info("Stop-market order placed: {} {} {} @ stop {} | id={} status={}", side, quantity, symbol, stop_price, order["order_id"], order["status"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_stop_market_order failed ({} {} {} stop {}): {}", side, quantity, symbol, stop_price, exc)
        raise


def place_take_profit_market_order(
    client: Client,
    symbol: str,
    side: str,
    quantity: Decimal,
    stop_price: Decimal,
) -> dict:
    """Place a take-profit market order (triggers a market fill when stop_price is hit).

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL" — typically opposite to the open position side.
        quantity: Order quantity in base asset units.
        stop_price: Price that triggers the take-profit.

    Returns:
        Normalised order dict. status will be "WORKING".
    """
    try:
        params: dict = dict(
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            algoType="CONDITIONAL",
            quantity=str(quantity),
            triggerPrice=str(stop_price),
            reduceOnly=True,
        )
        raw = with_retry(lambda: client.futures_create_algo_order(**params))
        order = _normalize_algo_order({**params, "origQty": params["quantity"], **raw})
        logger.info("Take-profit order placed: {} {} {} @ stop {} | id={} status={}", side, quantity, symbol, stop_price, order["order_id"], order["status"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_take_profit_market_order failed ({} {} {} stop {}): {}", side, quantity, symbol, stop_price, exc)
        raise


def place_stop_limit_order(
    client: Client,
    symbol: str,
    side: str,
    quantity: Decimal,
    stop_price: Decimal,
    limit_price: Decimal,
) -> dict:
    """Place a stop-limit order (triggers a limit fill when stop_price is hit).

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL" — typically opposite to the open position side.
        quantity: Order quantity in base asset units.
        stop_price: Price that triggers the order.
        limit_price: Limit price for the resulting order after trigger.

    Returns:
        Normalised order dict. status will be "WORKING".
    """
    try:
        params: dict = dict(
            symbol=symbol,
            side=side,
            type="STOP",
            algoType="CONDITIONAL",
            quantity=str(quantity),
            triggerPrice=str(stop_price),
            price=str(limit_price),
            reduceOnly=True,
        )
        raw = with_retry(lambda: client.futures_create_algo_order(**params))
        order = _normalize_algo_order({**params, "origQty": params["quantity"], **raw})
        logger.info("Stop-limit order placed: {} {} {} @ stop {} limit {} | id={} status={}", side, quantity, symbol, stop_price, limit_price, order["order_id"], order["status"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_stop_limit_order failed ({} {} {} stop {} limit {}): {}", side, quantity, symbol, stop_price, limit_price, exc)
        raise


def place_take_profit_limit_order(
    client: Client,
    symbol: str,
    side: str,
    quantity: Decimal,
    stop_price: Decimal,
    limit_price: Decimal,
) -> dict:
    """Place a take-profit limit order (triggers a limit fill when stop_price is hit).

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL" — typically opposite to the open position side.
        quantity: Order quantity in base asset units.
        stop_price: Price that triggers the take-profit.
        limit_price: Limit price for the resulting order after trigger.

    Returns:
        Normalised order dict. status will be "WORKING".
    """
    try:
        params: dict = dict(
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT",
            algoType="CONDITIONAL",
            quantity=str(quantity),
            triggerPrice=str(stop_price),
            price=str(limit_price),
            reduceOnly=True,
        )
        raw = with_retry(lambda: client.futures_create_algo_order(**params))
        order = _normalize_algo_order({**params, "origQty": params["quantity"], **raw})
        logger.info("Take-profit-limit order placed: {} {} {} @ stop {} limit {} | id={} status={}", side, quantity, symbol, stop_price, limit_price, order["order_id"], order["status"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_take_profit_limit_order failed ({} {} {} stop {} limit {}): {}", side, quantity, symbol, stop_price, limit_price, exc)
        raise


def cancel_algo_order(client: Client, symbol: str, algo_id: int) -> dict:
    """Cancel a conditional algo order by its algo ID.

    Use this for orders placed via any place_stop_* or place_take_profit_* function
    (they return is_algo=True, order_id is the algoId).

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        algo_id: The algoId returned when the order was placed.

    Returns:
        Normalised order dict of the cancelled algo order.
    """
    try:
        raw = with_retry(lambda: client.futures_cancel_algo_order(symbol=symbol, algoId=algo_id))
        order = _normalize_algo_order(raw)
        logger.info("Algo order {} cancelled for {}", algo_id, symbol)
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("cancel_algo_order failed (id={} {}): {}", algo_id, symbol, exc)
        raise
