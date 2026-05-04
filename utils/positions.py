"""Position management for Binance Futures."""

from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger

from utils.exchange import get_futures_positions
from utils.general import _normalize_order, with_retry


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
        open_positions = get_futures_positions(client, symbol=symbol)
        if not open_positions:
            logger.info("No open position for {} — nothing to close", symbol)
            return None

        pos = open_positions[0]
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
