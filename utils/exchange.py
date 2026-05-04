"""Authenticated Binance actions — all calls that require API credentials."""

import time
from decimal import Decimal
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger


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


def check_futures_connection(client: Client) -> bool:
    """Ping Binance and log the server time. Returns True if reachable.

    Args:
        client: Authenticated Binance client.

    Returns:
        True on success, False on any API or network error.
    """
    try:
        client.futures_ping()
        ts = client.futures_time()
        logger.info("Binance Futures connection OK — server time: {}", ts["serverTime"])
        return True
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("Binance Futures connection check failed: {}", exc)
        return False


def get_futures_balance(client: Client) -> list[dict]:
    """Return futures wallet balances for all assets with a non-zero balance.

    Args:
        client: Authenticated Binance client.

    Returns:
        List of dicts with keys: asset, balance, available, unrealized_pnl (all Decimal).
    """
    try:
        raw = client.futures_account_balance()
        balances = []
        for b in raw:
            balance = Decimal(b["balance"])
            if balance == 0:
                continue
            balances.append({
                "asset": b["asset"],
                "balance": balance,
                "available": Decimal(b["availableBalance"]),
                "unrealized_pnl": Decimal(b["crossUnPnl"]),
            })
        logger.info("Futures balances: {} assets", len(balances))
        return balances
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_futures_balance failed: {}", exc)
        raise


def get_futures_positions(client: Client, symbol: Optional[str] = None) -> list[dict]:
    """Return all open futures positions (non-zero positionAmt).

    Args:
        client: Authenticated Binance client.
        symbol: Optional trading pair to filter by, e.g. "BTCUSDT". If None,
            returns all symbols with an open position.

    Returns:
        List of dicts with keys: symbol, side (LONG/SHORT), amount, entry_price,
        mark_price, unrealized_pnl, leverage, liquidation_price (all prices Decimal).
    """
    try:
        kwargs = {"symbol": symbol} if symbol else {}
        raw = client.futures_position_information(**kwargs)
        positions = []
        for p in raw:
            amt = Decimal(p["positionAmt"])
            if amt == 0:
                continue
            positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "amount": amt,
                "entry_price": Decimal(p["entryPrice"]),
                "mark_price": Decimal(p["markPrice"]),
                "unrealized_pnl": Decimal(p["unRealizedProfit"]),
                "leverage": int(p["leverage"]) if p.get("leverage") else None,
                "liquidation_price": Decimal(p["liquidationPrice"]) if p.get("liquidationPrice") else None,
            })
        logger.info("Open futures positions: {}", len(positions))
        return positions
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_open_positions failed: {}", exc)
        raise


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


def get_symbol_info(client: Client, symbol: str) -> dict:
    """Fetch exchange filters for a futures symbol.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".

    Returns:
        Dict with keys: symbol, tick_size, step_size, min_qty, max_qty,
        min_notional, price_precision, qty_precision (precisions as int, others Decimal).
    """
    try:
        info = with_retry(lambda: client.futures_exchange_info())
        sym_info = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
        if sym_info is None:
            raise ValueError(f"Symbol {symbol} not found on Binance Futures")

        filters = {f["filterType"]: f for f in sym_info["filters"]}
        price_filter = filters.get("PRICE_FILTER", {})
        lot_size = filters.get("LOT_SIZE", {})
        min_notional = filters.get("MIN_NOTIONAL", {})

        tick_size = Decimal(price_filter.get("tickSize", "0"))
        step_size = Decimal(lot_size.get("stepSize", "0"))

        result = {
            "symbol": symbol,
            "tick_size": tick_size,
            "step_size": step_size,
            "min_qty": Decimal(lot_size.get("minQty", "0")),
            "max_qty": Decimal(lot_size.get("maxQty", "0")),
            "min_notional": Decimal(min_notional.get("notional", "0")),
            "price_precision": sym_info.get("pricePrecision", 0),
            "qty_precision": sym_info.get("quantityPrecision", 0),
        }
        logger.info("Symbol info for {}: tick_size={}, step_size={}", symbol, tick_size, step_size)
        return result
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_symbol_info failed for {}: {}", symbol, exc)
        raise


def set_leverage(client: Client, symbol: str, leverage: int) -> None:
    """Set the leverage for a futures symbol.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        leverage: Desired leverage multiplier (e.g. 10 for 10x).
    """
    try:
        with_retry(lambda: client.futures_change_leverage(symbol=symbol, leverage=leverage))
        logger.info("Leverage set to {}x for {}", leverage, symbol)
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("set_leverage failed for {}: {}", symbol, exc)
        raise


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


def place_stop_market_order(
    client: Client,
    symbol: str,
    side: str,
    quantity: Decimal,
    stop_price: Decimal,
    reduce_only: bool = True,
    position_side: Optional[str] = None,
) -> dict:
    """Place a stop-market order (triggers a market fill when stop_price is hit).

    Uses the Algo Order API (POST /fapi/v1/algoOrder), required since Binance migrated
    all conditional order types off /fapi/v1/order on 2025-12-09.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL" — typically opposite to the open position side.
        quantity: Order quantity in base asset units.
        stop_price: Price that triggers the order.
        reduce_only: One-way Mode only — if True, order can only reduce an existing position.
        position_side: Hedge Mode only — "LONG" or "SHORT". When set, reduce_only is ignored.

    Returns:
        Normalised order dict (see _normalize_algo_order).
    """
    try:
        params: dict = dict(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            algoType="CONDITIONAL",
            quantity=str(quantity),
            triggerPrice=str(stop_price),
        )
        if position_side is not None:
            params["positionSide"] = position_side
        else:
            params["reduceOnly"] = reduce_only
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
    reduce_only: bool = True,
    position_side: Optional[str] = None,
) -> dict:
    """Place a take-profit market order (triggers a market fill when stop_price is hit).

    Uses the Algo Order API (POST /fapi/v1/algoOrder), required since Binance migrated
    all conditional order types off /fapi/v1/order on 2025-12-09.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL" — typically opposite to the open position side.
        quantity: Order quantity in base asset units.
        stop_price: Price that triggers the take-profit.
        reduce_only: One-way Mode only — if True, order can only reduce an existing position.
        position_side: Hedge Mode only — "LONG" or "SHORT". When set, reduce_only is ignored.

    Returns:
        Normalised order dict (see _normalize_algo_order).
    """
    try:
        params: dict = dict(
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            algoType="CONDITIONAL",
            quantity=str(quantity),
            triggerPrice=str(stop_price),
        )
        if position_side is not None:
            params["positionSide"] = position_side
        else:
            params["reduceOnly"] = reduce_only
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
    reduce_only: bool = True,
) -> dict:
    """Place a stop-limit order (triggers a limit order when stop_price is hit).

    Prefer place_stop_market_order — a limit order risks not filling if price gaps
    through the limit. Uses the Algo Order API like all conditional order types.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL" — typically opposite to the open position side.
        quantity: Order quantity in base asset units.
        stop_price: Price that triggers the order.
        limit_price: Limit price after trigger (set below stop for SELL, above for BUY).
        reduce_only: If True, the order can only reduce an existing position.

    Returns:
        Normalised order dict (see _normalize_algo_order).
    """
    try:
        params = dict(
            symbol=symbol, side=side, type="STOP", algoType="CONDITIONAL",
            quantity=str(quantity), triggerPrice=str(stop_price), price=str(limit_price),
            reduceOnly=reduce_only,
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
    reduce_only: bool = True,
) -> dict:
    """Place a take-profit limit order (triggers a limit order when stop_price is hit).

    Prefer place_take_profit_market_order — a limit order risks not filling if price
    gaps through the limit. Uses the Algo Order API like all conditional order types.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        side: "BUY" or "SELL" — typically opposite to the open position side.
        quantity: Order quantity in base asset units.
        stop_price: Price that triggers the take-profit.
        limit_price: Limit price after trigger (set above stop for SELL, below for BUY).
        reduce_only: If True, the order can only reduce an existing position.

    Returns:
        Normalised order dict (see _normalize_algo_order).
    """
    try:
        params = dict(
            symbol=symbol, side=side, type="TAKE_PROFIT", algoType="CONDITIONAL",
            quantity=str(quantity), triggerPrice=str(stop_price), price=str(limit_price),
            reduceOnly=reduce_only,
        )
        raw = with_retry(lambda: client.futures_create_algo_order(**params))
        order = _normalize_algo_order({**params, "origQty": params["quantity"], **raw})
        logger.info("Take-profit limit order placed: {} {} {} @ stop {} limit {} | id={} status={}", side, quantity, symbol, stop_price, limit_price, order["order_id"], order["status"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_take_profit_limit_order failed ({} {} {} stop {} limit {}): {}", side, quantity, symbol, stop_price, limit_price, exc)
        raise


def get_open_orders(client: Client, symbol: str) -> list[dict]:
    """Return all open orders for a symbol, including conditional algo orders.

    Fetches from both /fapi/v1/openOrders (regular) and /fapi/v1/openAlgoOrders
    (conditional), since conditional orders no longer appear in the regular endpoint.

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
        orders = [_normalize_order(o) for o in regular] + [_normalize_algo_order(o) for o in algo]
        logger.info("Open orders for {} (regular={} algo={})", symbol, len(regular), len(algo))
        return orders
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_open_orders failed for {}: {}", symbol, exc)
        raise


def cancel_order(client: Client, symbol: str, order_id: int) -> dict:
    """Cancel a specific regular (non-algo) open order.

    For cancelling conditional orders placed via place_stop_market_order or
    place_take_profit_market_order, use cancel_algo_order instead.

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


def cancel_algo_order(client: Client, symbol: str, algo_id: int) -> dict:
    """Cancel a conditional algo order by its algo ID.

    Use this for orders placed via place_stop_market_order or
    place_take_profit_market_order (they return is_algo=True and their
    order_id is the algoId).

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
            with_retry(lambda algo_id=o["algoId"]: client.futures_cancel_algo_order(symbol=symbol, algoId=algo_id))
        logger.info("All open orders cancelled for {} (algo={})", symbol, len(algo_orders))
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("cancel_all_orders failed for {}: {}", symbol, exc)
        raise


def close_position(client: Client, symbol: str) -> Optional[dict]:
    """Close the open position for a symbol with a market order.

    Reads the current position size from Binance and places the opposite-side
    market order with reduceOnly=True. No-ops if there is no open position.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".

    Returns:
        Normalised order dict of the closing order, or None if no position exists.
    """
    try:
        positions = get_futures_positions(client, symbol=symbol)
        if not positions:
            logger.info("No open position for {} — nothing to close", symbol)
            return None

        pos = positions[0]
        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        quantity = abs(pos["amount"])

        raw = with_retry(lambda: client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="MARKET",
            quantity=str(quantity),
            reduceOnly=True,
        ))
        order = _normalize_order(raw)
        logger.info("Position closed: {} {} {} | id={} status={}", close_side, quantity, symbol, order["order_id"], order["status"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("close_position failed for {}: {}", symbol, exc)
        raise
