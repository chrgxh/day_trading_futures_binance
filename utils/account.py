"""Binance account state layer — connection, balances, positions, symbol info, leverage."""

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
        return positions
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_futures_positions failed: {}", exc)
        raise


def _parse_symbol_filters(sym_info: dict) -> dict:
    """Extract tick_size, step_size, and other trading filters from a raw exchange-info symbol entry."""
    filters = {f["filterType"]: f for f in sym_info["filters"]}
    price_filter = filters.get("PRICE_FILTER", {})
    lot_size = filters.get("LOT_SIZE", {})
    min_notional_f = filters.get("MIN_NOTIONAL", {})
    tick_size = Decimal(price_filter.get("tickSize", "0")).normalize()
    step_size = Decimal(lot_size.get("stepSize", "0")).normalize()
    return {
        "symbol": sym_info["symbol"],
        "tick_size": tick_size,
        "step_size": step_size,
        "min_qty": Decimal(lot_size.get("minQty", "0")),
        "max_qty": Decimal(lot_size.get("maxQty", "0")),
        "min_notional": Decimal(min_notional_f.get("notional", "0")),
        "price_precision": sym_info.get("pricePrecision", 0),
        "qty_precision": sym_info.get("quantityPrecision", 0),
    }


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
        sym_entry = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
        if sym_entry is None:
            raise ValueError(f"Symbol {symbol} not found on Binance Futures")
        result = _parse_symbol_filters(sym_entry)
        logger.info("Symbol info for {}: tick_size={}, step_size={}", symbol, result["tick_size"], result["step_size"])
        return result
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_symbol_info failed for {}: {}", symbol, exc)
        raise


def get_symbol_infos(client: Client, symbols: list[str]) -> dict[str, dict]:
    """Fetch exchange filters for multiple futures symbols in a single API call.

    Prefer this over calling get_symbol_info() in a loop — it avoids downloading
    the full exchange-info response once per symbol.

    Args:
        client: Authenticated Binance client.
        symbols: List of trading pairs, e.g. ["BTCUSDT", "ETHUSDT"].

    Returns:
        Dict keyed by symbol, each value matching the shape of get_symbol_info().

    Raises:
        ValueError: If any requested symbol is not found on Binance Futures.
    """
    try:
        info = with_retry(lambda: client.futures_exchange_info())
        symbol_set = set(symbols)
        result = {}
        for s in info["symbols"]:
            if s["symbol"] in symbol_set:
                parsed = _parse_symbol_filters(s)
                result[s["symbol"]] = parsed
                logger.info(
                    "Symbol info for {}: tick_size={}, step_size={}",
                    s["symbol"], parsed["tick_size"], parsed["step_size"],
                )
        missing = symbol_set - set(result.keys())
        if missing:
            raise ValueError(f"Symbols not found on Binance Futures: {missing}")
        return result
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_symbol_infos failed for {}: {}", symbols, exc)
        raise


def _normalize_trade(t: dict) -> dict:
    """Normalize a raw Binance account trade record to a consistent shape."""
    return {
        "trade_id": t["id"],
        "order_id": t["orderId"],
        "side": t["side"],
        "price": Decimal(t["price"]),
        "qty": Decimal(t["qty"]),
        "realized_pnl": Decimal(t["realizedPnl"]),
        "commission": Decimal(t["commission"]),
        "commission_asset": t["commissionAsset"],
        "time": t["time"],
        "is_maker": t.get("maker", False),
    }


def get_futures_recent_trades(
    client: Client,
    symbol: str,
    start_time_ms: int,
    limit: int = 50,
) -> list[dict]:
    """Return recent futures account trades for a symbol since start_time_ms.

    Used by TradeManager to compute realized P&L when a position closes or partially fills.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        start_time_ms: Epoch milliseconds — only trades at or after this time are returned.
        limit: Maximum number of trades to return (Binance cap: 1000).

    Returns:
        List of dicts with keys: trade_id, order_id, side, price (Decimal), qty (Decimal),
        realized_pnl (Decimal), commission (Decimal), commission_asset, time (ms), is_maker.
    """
    try:
        raw = with_retry(lambda: client.futures_account_trades(
            symbol=symbol,
            startTime=start_time_ms,
            limit=limit,
        ))
        trades = [_normalize_trade(t) for t in raw]
        logger.debug("Recent trades for {} since {}: {} trade(s)", symbol, start_time_ms, len(trades))
        return trades
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_futures_recent_trades failed for {}: {}", symbol, exc)
        raise


def get_futures_trades_for_range(
    client: Client,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
) -> list[dict]:
    """Return all futures account trades for a symbol within a UTC time range.

    Fetches up to 1000 trades per request (Binance cap). Sufficient for daily
    reporting on a day-trading bot; add pagination here if trade volume grows.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        start_time_ms: Range start, epoch milliseconds (inclusive).
        end_time_ms: Range end, epoch milliseconds (inclusive).

    Returns:
        List of dicts with keys: trade_id, order_id, side, price (Decimal), qty (Decimal),
        realized_pnl (Decimal), commission (Decimal), commission_asset, time (ms), is_maker.
    """
    try:
        raw = with_retry(lambda: client.futures_account_trades(
            symbol=symbol,
            startTime=start_time_ms,
            endTime=end_time_ms,
            limit=1000,
        ))
        trades = [_normalize_trade(t) for t in raw]
        logger.debug(
            "Trades for {} [{} – {}]: {} trade(s)",
            symbol, start_time_ms, end_time_ms, len(trades),
        )
        return trades
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_futures_trades_for_range failed for {}: {}", symbol, exc)
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
