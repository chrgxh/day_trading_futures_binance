"""Binance API wrapper. All exchange interactions go through this module."""

import time
from decimal import Decimal
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from loguru import logger


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


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def get_klines(client: Client, symbol: str, interval: str, limit: int = 100) -> list[list]:
    """Fetch candlestick (kline) data for a symbol.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        interval: Kline interval string, e.g. "1m", "5m", "1h".
        limit: Number of candles to return (max 1000).

    Returns:
        List of kline lists as returned by the Binance API.
    """
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        logger.debug("Fetched {} klines for {} @ {}", len(klines), symbol, interval)
        return klines
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_klines failed for {}: {}", symbol, exc)
        raise


def get_symbol_ticker(client: Client, symbol: str) -> Decimal:
    """Return the latest price for a symbol as a Decimal.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".

    Returns:
        Current price as Decimal.
    """
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        price = Decimal(ticker["price"])
        logger.debug("Ticker {} = {}", symbol, price)
        return price
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_symbol_ticker failed for {}: {}", symbol, exc)
        raise


# ---------------------------------------------------------------------------
# Account / balances
# ---------------------------------------------------------------------------

def get_asset_balance(client: Client, asset: str) -> Decimal:
    """Return the free (available) balance for an asset.

    Args:
        client: Authenticated Binance client.
        asset: Asset ticker, e.g. "USDT", "BTC".

    Returns:
        Free balance as Decimal.
    """
    try:
        balance = client.get_asset_balance(asset=asset)
        free = Decimal(balance["free"])
        logger.debug("Balance {} free = {}", asset, free)
        return free
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_asset_balance failed for {}: {}", asset, exc)
        raise


def get_open_orders(client: Client, symbol: str) -> list[dict]:
    """Return all open orders for a symbol.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".

    Returns:
        List of open order dicts.
    """
    try:
        orders = client.get_open_orders(symbol=symbol)
        logger.debug("Open orders for {}: {}", symbol, len(orders))
        return orders
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("get_open_orders failed for {}: {}", symbol, exc)
        raise


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def place_market_buy(
    client: Client,
    symbol: str,
    quantity: Decimal,
    dry_run: bool = True,
) -> Optional[dict]:
    """Place a market buy order.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        quantity: Quantity to buy (in base asset units).
        dry_run: If True, logs the order but does not submit it.

    Returns:
        Order response dict, or None in dry-run mode.
    """
    if dry_run:
        logger.info("[DRY RUN] market BUY {} qty={}", symbol, quantity)
        return None
    try:
        order = client.order_market_buy(symbol=symbol, quantity=str(quantity))
        logger.info("Market BUY placed: {} qty={} orderId={}", symbol, quantity, order["orderId"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_market_buy failed for {} qty={}: {}", symbol, quantity, exc)
        raise


def place_market_sell(
    client: Client,
    symbol: str,
    quantity: Decimal,
    dry_run: bool = True,
) -> Optional[dict]:
    """Place a market sell order.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        quantity: Quantity to sell (in base asset units).
        dry_run: If True, logs the order but does not submit it.

    Returns:
        Order response dict, or None in dry-run mode.
    """
    if dry_run:
        logger.info("[DRY RUN] market SELL {} qty={}", symbol, quantity)
        return None
    try:
        order = client.order_market_sell(symbol=symbol, quantity=str(quantity))
        logger.info("Market SELL placed: {} qty={} orderId={}", symbol, quantity, order["orderId"])
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_market_sell failed for {} qty={}: {}", symbol, quantity, exc)
        raise


def place_stop_loss(
    client: Client,
    symbol: str,
    quantity: Decimal,
    stop_price: Decimal,
    dry_run: bool = True,
) -> Optional[dict]:
    """Place a stop-loss (STOP_LOSS_LIMIT) sell order.

    Args:
        client: Authenticated Binance client.
        symbol: Trading pair, e.g. "BTCUSDT".
        quantity: Quantity to sell on trigger.
        stop_price: Price at which the stop triggers.
        dry_run: If True, logs but does not submit.

    Returns:
        Order response dict, or None in dry-run mode.
    """
    limit_price = stop_price * Decimal("0.999")  # 0.1% slippage buffer
    if dry_run:
        logger.info(
            "[DRY RUN] STOP_LOSS_LIMIT SELL {} qty={} stop={} limit={}",
            symbol, quantity, stop_price, limit_price,
        )
        return None
    try:
        order = client.create_order(
            symbol=symbol,
            side=Client.SIDE_SELL,
            type=Client.ORDER_TYPE_STOP_LOSS_LIMIT,
            timeInForce=Client.TIME_IN_FORCE_GTC,
            quantity=str(quantity),
            stopPrice=str(stop_price),
            price=str(limit_price),
        )
        logger.info(
            "Stop-loss placed: {} qty={} stop={} orderId={}",
            symbol, quantity, stop_price, order["orderId"],
        )
        return order
    except (BinanceAPIException, BinanceRequestException) as exc:
        logger.error("place_stop_loss failed for {}: {}", symbol, exc)
        raise


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

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
