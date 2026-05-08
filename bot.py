"""Entry point. Orchestrates the bot loop and enforces risk controls."""

import os
import queue
import sys
import time
from decimal import Decimal

import yaml
from dotenv import load_dotenv
from loguru import logger

from utils import account, algo_orders, general, market, orders
from utils.general import PostOnlyRejected
from utils import positions as pos_utils
from utils.indicators import Position, Signal, TradeSignal
from strategies import STRATEGIES


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def configure_logging(log_file: str, level: str, rotation: str, retention: str) -> None:
    logger.remove()
    logger.add(sys.stdout, level=level)
    logger.add(log_file, level=level, rotation=rotation, retention=retention, enqueue=True)


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_env() -> dict:
    load_dotenv()
    required = ("BINANCE_API_KEY", "BINANCE_API_SECRET")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logger.error("Missing required environment variables: {}", missing)
        sys.exit(1)
    return {
        "api_key": os.environ["BINANCE_API_KEY"],
        "api_secret": os.environ["BINANCE_API_SECRET"],
        "testnet": os.getenv("BINANCE_TESTNET", "true").lower() == "true",
    }


def setup_symbols(client, symbols: list[str], leverage: int) -> dict[str, dict]:
    """Set leverage and fetch symbol info for every symbol."""
    sym_info = {}
    for symbol in symbols:
        account.set_leverage(client, symbol, leverage)
        sym_info[symbol] = account.get_symbol_info(client, symbol)
    return sym_info


def recover_positions(client, symbols: list[str]) -> dict[str, Position]:
    """Query Binance for open positions so state survives restarts."""
    open_positions = {s: Position.NONE for s in symbols}
    for pos in account.get_futures_positions(client):
        if pos["symbol"] in open_positions:
            open_positions[pos["symbol"]] = Position[pos["side"]]
            logger.info("Recovered open {} position for {}", pos["side"], pos["symbol"])
    return open_positions


def _round_price(price: Decimal, tick_size: Decimal) -> Decimal:
    return (price / tick_size).to_integral_value() * tick_size


def attempt_limit_entry(
    client,
    symbol: str,
    side: str,
    quantity: Decimal,
    tick_size: Decimal,
    signal_price: Decimal,
    timeout_secs: int,
    max_deviation_pct: Decimal,
    max_retries: int,
) -> dict | None:
    """Try to fill a GTX (post-only) limit entry order, retrying if the order times out or is rejected.

    Places a limit order at the current mark price. If the order isn't filled within
    timeout_secs, it is cancelled and a new attempt is made at the new mark price —
    provided the price hasn't drifted more than max_deviation_pct from signal_price.
    A GTX post-only rejection (order would have been taker) also triggers an immediate retry.

    Returns the filled order dict on success, or None if entry was aborted.
    """
    for attempt in range(1, max_retries + 1):
        current_price = market.get_futures_mark_price(client, symbol)
        deviation = abs(current_price - signal_price) / signal_price

        if deviation > max_deviation_pct:
            logger.warning(
                "{} Limit entry aborted: price moved {:.4f}% from signal price {} (max {:.4f}%)",
                symbol, float(deviation * 100), signal_price, float(max_deviation_pct * 100),
            )
            return None

        limit_price = _round_price(current_price, tick_size)
        logger.info(
            "{} Placing GTX limit {} {} @ {} (attempt {}/{})",
            symbol, side, quantity, limit_price, attempt, max_retries,
        )

        try:
            order = orders.place_limit_order(client, symbol, side, quantity, limit_price, time_in_force="GTX")
        except PostOnlyRejected:
            logger.info("{} GTX order rejected (would be taker), retrying immediately", symbol)
            continue
        except Exception:
            raise

        # Poll for fill until timeout
        deadline = time.monotonic() + timeout_secs
        filled_order: dict | None = None
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                status = orders.get_order(client, symbol, order["order_id"])
            except Exception as exc:
                logger.warning("{} Could not poll order {}: {}", symbol, order["order_id"], exc)
                break

            if status["status"] == "FILLED":
                filled_order = status
                break
            if status["status"] in ("CANCELED", "EXPIRED"):
                logger.info("{} Limit order {} was cancelled/expired — retrying", symbol, order["order_id"])
                break

        if filled_order is not None:
            logger.info("{} Limit entry filled @ {} (attempt {})", symbol, limit_price, attempt)
            return filled_order

        # Cancel the order if it's still open before retrying
        try:
            latest = orders.get_order(client, symbol, order["order_id"])
            if latest["status"] not in ("CANCELED", "EXPIRED", "FILLED"):
                orders.cancel_order(client, symbol, order["order_id"])
                logger.info("{} Cancelled unfilled limit order {} after {}s timeout", symbol, order["order_id"], timeout_secs)
        except Exception as exc:
            logger.warning("{} Could not cancel order {}: {}", symbol, order["order_id"], exc)

    logger.warning("{} Limit entry gave up after {} attempts", symbol, max_retries)
    return None


def execute_signal(
    client,
    symbol: str,
    signal: TradeSignal,
    max_usdt: Decimal,
    sym_info: dict,
    open_positions: dict[str, Position],
    stop_order_ids: dict[str, list[int]],
    trailing_tp_order_ids: dict[str, int | None],
    sl_limit_pct: Decimal,
    sl_market_pct: Decimal,
    ttp_activation_pct: Decimal,
    ttp_callback_rate: Decimal,
    entry_timeout_secs: int,
    max_entry_deviation_pct: Decimal,
    entry_max_retries: int,
) -> None:
    """Place the order for a non-HOLD signal, set stop losses + trailing TP on open, cancel on close."""
    if signal.signal in (Signal.OPEN_LONG, Signal.OPEN_SHORT):
        signal_price = signal.entry_price or market.get_futures_mark_price(client, symbol)
        step_size = sym_info["step_size"]
        tick_size = sym_info["tick_size"]
        quantity = (max_usdt / signal_price // step_size) * step_size
        is_long = signal.signal == Signal.OPEN_LONG
        side = "BUY" if is_long else "SELL"
        stop_side = "SELL" if is_long else "BUY"

        filled_order = attempt_limit_entry(
            client, symbol, side, quantity, tick_size,
            signal_price, entry_timeout_secs, max_entry_deviation_pct, entry_max_retries,
        )

        if filled_order is None:
            logger.info("{} Limit entry aborted — no position opened.", symbol)
            return

        open_positions[symbol] = Position.LONG if is_long else Position.SHORT

        # Base stop losses and trailing TP on the actual fill price
        fill_price = filled_order["price"] if filled_order["price"] > 0 else signal_price

        if is_long:
            sl_limit_trigger = _round_price(fill_price * (1 - sl_limit_pct), tick_size)
            sl_market_trigger = _round_price(fill_price * (1 - sl_market_pct), tick_size)
            ttp_activation = _round_price(fill_price * (1 + ttp_activation_pct), tick_size)
        else:
            sl_limit_trigger = _round_price(fill_price * (1 + sl_limit_pct), tick_size)
            sl_market_trigger = _round_price(fill_price * (1 + sl_market_pct), tick_size)
            ttp_activation = _round_price(fill_price * (1 - ttp_activation_pct), tick_size)

        sl_limit_order = algo_orders.place_stop_limit_order(
            client, symbol, stop_side, quantity, sl_limit_trigger, sl_limit_trigger
        )
        sl_market_order = algo_orders.place_stop_market_order(
            client, symbol, stop_side, quantity, sl_market_trigger
        )
        stop_order_ids[symbol] = [sl_limit_order["order_id"], sl_market_order["order_id"]]

        ttp_order = orders.place_trailing_stop_order(
            client, symbol, stop_side, quantity, ttp_callback_rate, ttp_activation
        )
        trailing_tp_order_ids[symbol] = ttp_order["order_id"]

    elif signal.signal == Signal.CLOSE:
        for algo_id in stop_order_ids.get(symbol, []):
            try:
                algo_orders.cancel_algo_order(client, symbol, algo_id)
            except Exception as exc:
                logger.warning("Could not cancel stop order {} for {}: {}", algo_id, symbol, exc)
        stop_order_ids[symbol] = []

        ttp_id = trailing_tp_order_ids.get(symbol)
        if ttp_id is not None:
            try:
                algo_orders.cancel_algo_order(client, symbol, ttp_id)
            except Exception as exc:
                logger.warning("Could not cancel trailing TP order {} for {}: {}", ttp_id, symbol, exc)
            trailing_tp_order_ids[symbol] = None

        pos_utils.close_position(client, symbol)
        open_positions[symbol] = Position.NONE


# ---------------------------------------------------------------------------
# Risk controls
# ---------------------------------------------------------------------------

class RiskGuard:
    """Enforces per-trade and daily loss limits before any order is placed."""

    def __init__(self, max_position_usdt: float, max_daily_loss_usdt: float, kill_switch: bool):
        self.max_position_usdt = Decimal(str(max_position_usdt))
        self.max_daily_loss_usdt = Decimal(str(max_daily_loss_usdt))
        self.kill_switch = kill_switch
        self.daily_loss: Decimal = Decimal("0")

    def check(self) -> bool:
        """Return True if the trade is permitted under current risk limits."""
        if self.kill_switch:
            logger.warning("Kill switch is active — all trades blocked.")
            return False
        if self.daily_loss >= self.max_daily_loss_usdt:
            logger.warning("Daily loss limit reached ({} / {}). Blocking trade.",
                           self.daily_loss, self.max_daily_loss_usdt)
            return False
        return True

    def record_loss(self, amount_usdt: Decimal) -> None:
        self.daily_loss += amount_usdt
        logger.info("Daily loss updated: {} / {}", self.daily_loss, self.max_daily_loss_usdt)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    try:
        _run()
    except Exception as exc:
        logger.critical("Bot crashed: {}", exc)
        general.send_crash_email(exc)
        raise


def _run() -> None:
    cfg = load_config()
    env = load_env()
    configure_logging(**cfg["logging"])

    logger.info("Bot starting. testnet={}", env["testnet"])

    client = general.build_client(env["api_key"], env["api_secret"], testnet=env["testnet"])
    risk = RiskGuard(
        max_position_usdt=cfg["risk"]["max_position_size_usdt"],
        max_daily_loss_usdt=cfg["risk"]["max_daily_loss_usdt"],
        kill_switch=cfg["risk"]["kill_switch"],
    )

    symbols: list[str] = cfg["trading"]["symbols"]
    interval: str = cfg["trading"]["interval"]
    strategy_fn = STRATEGIES[cfg["trading"]["strategy"]]
    strategy_params: dict = cfg["trading"].get("strategy_params", {})
    logger.info("Strategy: {}", cfg["trading"]["strategy"])

    sl_limit_pct = Decimal(str(cfg["risk"].get("stop_loss_limit_pct", 1.0))) / 100
    sl_market_pct = Decimal(str(cfg["risk"].get("stop_loss_market_pct", 2.0))) / 100
    ttp_activation_pct = Decimal(str(cfg["risk"].get("trailing_take_profit_activation_pct", 1.0))) / 100
    ttp_callback_rate = Decimal(str(cfg["risk"].get("trailing_take_profit_callback_rate", 2.0)))

    entry_cfg = cfg.get("entry", {})
    entry_timeout_secs: int = int(entry_cfg.get("limit_order_timeout_secs", 20))
    max_entry_deviation_pct = Decimal(str(entry_cfg.get("max_price_deviation_pct", 0.3))) / 100
    entry_max_retries: int = int(entry_cfg.get("max_retries", 3))

    sym_info = setup_symbols(client, symbols, cfg["risk"]["leverage"])
    open_positions = recover_positions(client, symbols)
    stop_order_ids: dict[str, list[int]] = {s: [] for s in symbols}
    trailing_tp_order_ids: dict[str, int | None] = {s: None for s in symbols}

    # Pre-fetch candle history so strategies have enough data on the first tick.
    candle_limit: int = int(cfg["trading"].get("candle_limit", 200))
    candle_buffers: dict[str, list[dict]] = {}
    for symbol in symbols:
        candle_buffers[symbol] = market.get_futures_ohlcv(client, symbol, interval, limit=candle_limit)
        logger.info("Prefetched {} candles for {}", len(candle_buffers[symbol]), symbol)

    # WebSocket callbacks run in background threads — route events through a queue
    # so all state mutations happen on the main thread.
    event_queue: queue.SimpleQueue = queue.SimpleQueue()

    def on_closed_candle(symbol: str, candle: dict) -> None:
        event_queue.put((symbol, candle))

    twm = market.start_kline_streams(
        env["api_key"], env["api_secret"], env["testnet"],
        symbols, interval, on_closed_candle,
    )

    try:
        while True:
            symbol, candle = event_queue.get()

            buf = candle_buffers[symbol]
            # The last REST candle may have been open at prefetch time; replace it if
            # the closed WS candle covers the same period, otherwise append.
            if buf and candle["open_time"] == buf[-1]["open_time"]:
                buf[-1] = candle
            else:
                buf.append(candle)
                if len(buf) > candle_limit:
                    buf.pop(0)

            try:
                position = open_positions[symbol]
                signal = strategy_fn(buf, symbol, position, strategy_params)

                logger.info("{} [{}] {} — {}", symbol, position.value, signal.signal.value, signal.reason)

                if signal.signal == Signal.HOLD or not risk.check():
                    continue

                execute_signal(client, symbol, signal, risk.max_position_usdt,
                               sym_info[symbol], open_positions, stop_order_ids,
                               trailing_tp_order_ids, sl_limit_pct, sl_market_pct,
                               ttp_activation_pct, ttp_callback_rate,
                               entry_timeout_secs, max_entry_deviation_pct, entry_max_retries)

                # Immediately re-evaluate on the same candle after a close.
                # Handles trend reversals (close short → open long) and RSI flush re-entries
                # without waiting for the next candle.
                if signal.signal == Signal.CLOSE and open_positions[symbol] == Position.NONE and risk.check():
                    reentry = strategy_fn(buf, symbol, Position.NONE, strategy_params)
                    logger.info("{} [NONE] {} — {} (re-entry check)", symbol, reentry.signal.value, reentry.reason)
                    if reentry.signal in (Signal.OPEN_LONG, Signal.OPEN_SHORT):
                        execute_signal(client, symbol, reentry, risk.max_position_usdt,
                                       sym_info[symbol], open_positions, stop_order_ids,
                                       trailing_tp_order_ids, sl_limit_pct, sl_market_pct,
                                       ttp_activation_pct, ttp_callback_rate,
                                       entry_timeout_secs, max_entry_deviation_pct, entry_max_retries)

            except Exception as exc:
                logger.exception("Error processing {}: {}", symbol, exc)

    finally:
        twm.stop()
        logger.info("WebSocket streams stopped.")


if __name__ == "__main__":
    run()
