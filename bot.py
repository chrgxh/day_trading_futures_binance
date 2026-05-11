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
from utils.general import round_price

from utils import positions as pos_utils
from utils.indicators import Position, Signal, TradeSignal, adx as compute_adx, rsi as compute_rsi, interval_to_minutes
from utils.trade_manager import TradeManager
from utils.pnl_reporter import DailyPnLReporter
from strategies import STRATEGIES


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def configure_logging(log_file: str, level: str, rotation: str, retention: str, debug_log_file: str | None = None) -> None:
    logger.remove()
    logger.add(sys.stdout, level=level)
    logger.add(log_file, level=level, rotation=rotation, retention=retention, enqueue=True)
    if debug_log_file:
        logger.add(debug_log_file, filter=lambda r: r["level"].no < logger.level("INFO").no, rotation=rotation, retention=retention, enqueue=True)


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_env() -> dict:
    env_path = ".env"
    if os.path.exists(env_path):
        load_dotenv(env_path)
    required = (
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "RESEND_API_KEY",
        "CRASH_NOTIFY_EMAIL",
        "CRASH_NOTIFY_FROM_EMAIL",
    )
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logger.error("Bot cannot start — missing required environment variables: {}", missing)
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


def ioc_entry(
    client,
    symbol: str,
    side: str,
    quantity: Decimal,
    tick_size: Decimal,
    signal_price: Decimal,
    max_deviation_pct: Decimal,
) -> dict | None:
    """IOC limit entry: chases best ask (BUY) / best bid (SELL) until filled or price drifts too far.

    Retries with a fresh price on each attempt. The only exit conditions are a full fill or price
    drifting beyond max_deviation_pct from signal_price. On partial fill with price drift, returns
    a partial fill dict so stop/TP orders are placed on the live position.

    Returns the filled order dict on success, or None if entry was aborted.
    """
    def _within_deviation(ref: Decimal) -> bool:
        dev = float(abs(ref - signal_price) / signal_price)
        if dev > float(max_deviation_pct):
            logger.warning(
                "{} Entry aborted: price moved {:.4f}% from signal {} (max {:.4f}%)",
                symbol, dev * 100, signal_price, float(max_deviation_pct * 100),
            )
            return False
        return True

    # Track cumulative filled quantity so each IOC only requests the remaining amount.
    # Without this, a partial fill followed by a full-size IOC creates an oversized position.
    filled_qty = Decimal("0")
    remaining_qty = quantity
    attempt = 0
    while True:
        attempt += 1
        best_bid, best_ask = market.get_futures_best_bid_ask(client, symbol)
        aggressive_ref = best_ask if side == "BUY" else best_bid
        if not _within_deviation(aggressive_ref):
            if filled_qty > 0:
                # Price drifted but we already have a partial fill on Binance.
                # Return partial fill info so execute_signal places stops — a live
                # position must never be left without protection.
                logger.warning(
                    "{} IOC: price drift abort with {} already filled — "
                    "returning partial fill so stop/TP orders are placed.",
                    symbol, filled_qty,
                )
                return {"price": signal_price, "executed_qty": filled_qty, "status": "PARTIALLY_FILLED"}
            return None

        limit_price = round_price(aggressive_ref, tick_size)
        logger.info(
            "{} Placing IOC limit {} {} @ {} (attempt {})",
            symbol, side, remaining_qty, limit_price, attempt,
        )
        ioc_order = orders.place_limit_order(client, symbol, side, remaining_qty, limit_price, time_in_force="IOC")

        time.sleep(0.5)
        try:
            status = orders.get_order(client, symbol, ioc_order["order_id"])
            this_fill = status.get("executed_qty", Decimal("0"))
            if status["status"] in ("FILLED", "PARTIALLY_FILLED") and this_fill > 0:
                filled_qty += this_fill
                remaining_qty = quantity - filled_qty
                logger.info(
                    "{} IOC {} @ {} (attempt {}): filled={} cumulative={}/{}",
                    symbol, status["status"], limit_price, attempt, this_fill, filled_qty, quantity,
                )
            if status["status"] == "FILLED" or remaining_qty <= 0:
                logger.info("{} IOC fully filled after {} attempt(s)", symbol, attempt)
                # Patch executed_qty to reflect cumulative fill across all IOC attempts.
                status["executed_qty"] = filled_qty
                return status
        except Exception as exc:
            logger.warning("{} Could not check IOC order {}: {}", symbol, ioc_order["order_id"], exc)


def execute_signal(
    client,
    symbol: str,
    signal: TradeSignal,
    max_usdt: Decimal,
    sym_info: dict,
    trade_manager: TradeManager,
    sl_limit_pct: Decimal,
    sl_market_pct: Decimal,
    tp_limit_pct: Decimal,
    ttp_activation_pct: Decimal,
    ttp_callback_rate: Decimal,
    max_entry_deviation_pct: Decimal,
) -> None:
    """Place the order for a non-HOLD signal, set stop losses + TP orders on open, cancel on close."""
    if signal.signal in (Signal.OPEN_LONG, Signal.OPEN_SHORT):
        signal_price = signal.entry_price or market.get_futures_mark_price(client, symbol)
        step_size = sym_info["step_size"]
        tick_size = sym_info["tick_size"]
        quantity = (max_usdt / signal_price // step_size) * step_size
        is_long = signal.signal == Signal.OPEN_LONG
        side = "BUY" if is_long else "SELL"
        stop_side = "SELL" if is_long else "BUY"

        filled_order = ioc_entry(
            client, symbol, side, quantity, tick_size,
            signal_price, max_entry_deviation_pct,
        )

        if filled_order is None:
            logger.info("{} Limit entry aborted — no position opened.", symbol)
            return

        # Use actual fill size for reduceOnly stop/TP orders.
        # For a partial IOC fill (deviation abort), executed_qty < quantity; sizing stops
        # for the full quantity would cause Binance to reject them as reduceOnly violations.
        fill_qty = filled_order.get("executed_qty") or quantity
        if fill_qty <= 0:
            fill_qty = quantity

        # Base all exit orders on the actual fill price
        fill_price = filled_order["price"] if filled_order["price"] > 0 else signal_price

        if is_long:
            sl_limit_trigger = round_price(fill_price * (1 - sl_limit_pct), tick_size)
            sl_market_trigger = round_price(fill_price * (1 - sl_market_pct), tick_size)
            ttp_activation = round_price(fill_price * (1 + ttp_activation_pct), tick_size)
            tp_limit_price = round_price(fill_price * (1 + tp_limit_pct), tick_size)
        else:
            sl_limit_trigger = round_price(fill_price * (1 + sl_limit_pct), tick_size)
            sl_market_trigger = round_price(fill_price * (1 + sl_market_pct), tick_size)
            ttp_activation = round_price(fill_price * (1 - ttp_activation_pct), tick_size)
            tp_limit_price = round_price(fill_price * (1 - tp_limit_pct), tick_size)

        sl_limit_order = algo_orders.place_stop_limit_order(
            client, symbol, stop_side, fill_qty, sl_limit_trigger, sl_limit_trigger
        )
        sl_market_order = algo_orders.place_stop_market_order(
            client, symbol, stop_side, fill_qty, sl_market_trigger
        )
        ttp_order = orders.place_trailing_stop_order(
            client, symbol, stop_side, fill_qty, ttp_callback_rate, ttp_activation
        )
        tp_limit_order = orders.place_tp_limit_order(
            client, symbol, stop_side, fill_qty, tp_limit_price
        )

        trade_manager.register_trade(
            symbol=symbol,
            position=Position.LONG if is_long else Position.SHORT,
            size=fill_qty,
            entry_price=fill_price,
            tick_size=tick_size,
            stop_ids=[sl_limit_order["order_id"], sl_market_order["order_id"]],
            sl_limit_price=sl_limit_trigger,
            sl_market_price=sl_market_trigger,
            ttp_id=ttp_order["order_id"],
            tp_limit_id=tp_limit_order["order_id"],
        )

    elif signal.signal == Signal.CLOSE:
        trade_manager.close_trade(symbol)
        pos_utils.close_position(client, symbol)


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
    tp_limit_pct = Decimal(str(cfg["risk"].get("take_profit_limit_pct", 3.0))) / 100
    ttp_activation_pct = Decimal(str(cfg["risk"].get("trailing_take_profit_activation_pct", 1.0))) / 100
    ttp_callback_rate = Decimal(str(cfg["risk"].get("trailing_take_profit_callback_rate", 2.0)))
    sl_profit_trigger_pct = Decimal(str(cfg["risk"].get("sl_profit_trigger_pct", 1.0))) / 100
    sl_profit_lock_pct = Decimal(str(cfg["risk"].get("sl_profit_lock_pct", 0.5))) / 100
    sl_profit_market_lock_pct = Decimal(str(cfg["risk"].get("sl_profit_market_lock_pct", 0.3))) / 100

    entry_cfg = cfg.get("entry", {})
    max_entry_deviation_pct = Decimal(str(entry_cfg.get("max_price_deviation_pct", 0.3))) / 100

    sym_info = setup_symbols(client, symbols, cfg["risk"]["leverage"])

    tm_poll_secs: int = int(cfg.get("trade_manager", {}).get("poll_interval_secs", 10))
    trade_manager = TradeManager(
        client,
        poll_interval_secs=tm_poll_secs,
        sl_profit_trigger_pct=sl_profit_trigger_pct,
        sl_profit_lock_pct=sl_profit_lock_pct,
        sl_profit_market_lock_pct=sl_profit_market_lock_pct,
    )
    trade_manager.start()

    pnl_csv_file = cfg["reporting"]["pnl_csv_file"]
    pnl_reporter = DailyPnLReporter(client, symbols, pnl_csv_file)
    pnl_reporter.start()

    # Register any positions already open on Binance so TradeManager can detect
    # external closes between candles. Order IDs are not recoverable on restart.
    recovered = recover_positions(client, symbols)
    for symbol, pos in recovered.items():
        if pos != Position.NONE:
            pos_list = account.get_futures_positions(client, symbol=symbol)
            if pos_list:
                p = pos_list[0]
                trade_manager.register_trade(
                    symbol=symbol,
                    position=pos,
                    size=abs(p["amount"]),
                    entry_price=p["entry_price"],
                    tick_size=sym_info[symbol]["tick_size"],
                    stop_ids=[],
                    sl_limit_price=Decimal("0"),
                    sl_market_price=Decimal("0"),
                    ttp_id=None,
                    tp_limit_id=None,
                    has_order_details=False,
                )
                logger.warning(
                    "{} Recovered {} position registered — stop/TP order IDs unknown.",
                    symbol, pos.value,
                )

    # Pre-fetch candle history so strategies have enough data on the first tick.
    # Auto-compute enough candles for 200 complete 1h bars at the chosen interval,
    # unless the config explicitly overrides it.
    _cfg_limit = cfg["trading"].get("candle_limit")
    if _cfg_limit:
        candle_limit = int(_cfg_limit)
    else:
        interval_min = interval_to_minutes(interval)
        candle_limit = (200 * 60 // interval_min) + 50
    logger.info("Candle limit: {} (interval={})", candle_limit, interval)
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
                position = trade_manager.get_position(symbol)
                signal = strategy_fn(buf, symbol, position, strategy_params)

                logger.info("{} [{}] {} — {}", symbol, position.value, signal.signal.value, signal.reason)

                if signal.signal == Signal.HOLD and position != Position.NONE:
                    closes = [c["close"] for c in buf]
                    adx_series = compute_adx(buf, strategy_params.get("adx_period", 14))
                    current_adx = adx_series[-1] if adx_series else Decimal("0")
                    current_rsi = compute_rsi(closes, strategy_params.get("rsi_period", 14))
                    if trade_manager.tick_stagnation(
                        symbol=symbol,
                        current_price=closes[-1],
                        current_adx=current_adx,
                        current_rsi=current_rsi,
                        min_adx=Decimal(str(strategy_params.get("min_adx", "25"))),
                        rsi_long_low=Decimal(str(strategy_params.get("rsi_long_low", "50"))),
                        rsi_short_high=Decimal(str(strategy_params.get("rsi_short_high", "50"))),
                        stagnation_candles=int(strategy_params.get("stagnation_candles", 4)),
                        stagnation_min_pct=Decimal(str(strategy_params.get("stagnation_min_pct", "2.0"))),
                    ):
                        signal = TradeSignal(signal=Signal.CLOSE, symbol=symbol, reason="stagnation exit — momentum decayed")
                        logger.info("{} Stagnation exit triggered — overriding HOLD to CLOSE.", symbol)

                if signal.signal == Signal.HOLD or not risk.check():
                    continue

                execute_signal(client, symbol, signal, risk.max_position_usdt,
                               sym_info[symbol], trade_manager, sl_limit_pct, sl_market_pct,
                               tp_limit_pct, ttp_activation_pct, ttp_callback_rate,
                               max_entry_deviation_pct)

                # Immediately re-evaluate on the same candle after a close.
                # Handles trend reversals (close short → open long) and RSI flush re-entries
                # without waiting for the next candle.
                if signal.signal == Signal.CLOSE and trade_manager.get_position(symbol) == Position.NONE and risk.check():
                    reentry = strategy_fn(buf, symbol, Position.NONE, strategy_params)
                    logger.info("{} [NONE] {} — {} (re-entry check)", symbol, reentry.signal.value, reentry.reason)
                    if reentry.signal in (Signal.OPEN_LONG, Signal.OPEN_SHORT):
                        execute_signal(client, symbol, reentry, risk.max_position_usdt,
                                       sym_info[symbol], trade_manager, sl_limit_pct, sl_market_pct,
                                       tp_limit_pct, ttp_activation_pct, ttp_callback_rate,
                                       max_entry_deviation_pct)

            except Exception as exc:
                logger.exception("Error processing {}: {}", symbol, exc)

    finally:
        trade_manager.stop()
        twm.stop()
        logger.info("WebSocket streams stopped.")


if __name__ == "__main__":
    run()
