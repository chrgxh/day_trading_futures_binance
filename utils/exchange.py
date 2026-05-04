"""Authenticated Binance account layer — connection, balances, positions, symbol info."""

from decimal import Decimal
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger

from utils.general import with_retry


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
        leverage and liquidation_price may be None depending on the API response version.
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
        logger.error("get_futures_positions failed: {}", exc)
        raise


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

        tick_size = Decimal(price_filter.get("tickSize", "0")).normalize()
        step_size = Decimal(lot_size.get("stepSize", "0")).normalize()

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
