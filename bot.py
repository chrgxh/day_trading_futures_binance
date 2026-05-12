"""Entry point. Orchestrates the bot loop and enforces risk controls."""

import os
import queue
import sys
import time
from decimal import Decimal

import yaml
from dotenv import load_dotenv
from loguru import logger

from utils import account, algo_orders, general, market, orders, positions as pos_utils
from utils.general import round_price
from utils.indicators import Position, Signal, TradeSignal, interval_to_minutes
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
    """Set leverage for every symbol and fetch all symbol info in a single exchange-info call."""
    for symbol in symbols:
        account.set_leverage(client, symbol, leverage)
    return account.get_symbol_infos(client, symbols)


def recover_positions(client, symbols: list[str]) -> dict[str, dict | None]:
    """Query Binance for open positions so state survives restarts.

    Returns a dict keyed by symbol; value is the full position dict for open
    positions, None for flat symbols.
    """
    result: dict[str, dict | None] = {s: None for s in symbols}
    for pos in account.get_futures_positions(client):
        if pos["symbol"] in result:
            result[pos["symbol"]] = pos
            logger.info("Recovered open {} position for {}", pos["side"], pos["symbol"])
    return result


def _register_recovered_position(
    client,
    symbol: str,
    pos: Position,
    pos_data: dict,
    sym_info: dict,
    trade_manager: TradeManager,
) -> None:
    """Register a recovered position with TradeManager, matching open exit orders by type and side."""
    sl_limit_o = sl_market_o = ttp_o = tp_limit_o = None
    try:
        open_orders = orders.get_open_orders(client, symbol)
        exit_side = "SELL" if pos == Position.LONG else "BUY"
        # Binance does not populate `type` on algo order query responses, so match
        # by price structure instead: stop-limit has both price and stop_price set;
        # stop-market has only stop_price; trailing stop has neither. This is safe
        # because the bot only ever places these three algo order types per position,
        # all reduceOnly on the exit side — no other algo order can collide.
        algo_exits = [o for o in open_orders if o["is_algo"] and o["side"] == exit_side]
        sl_limit_o = next((o for o in algo_exits if o["price"] > 0 and o["stop_price"] > 0), None)
        sl_market_o = next((o for o in algo_exits if o["price"] == 0 and o["stop_price"] > 0), None)
        ttp_o = next((o for o in algo_exits if o["price"] == 0 and o["stop_price"] == 0), None)
        tp_limit_o = next((o for o in open_orders if not o["is_algo"] and o["type"] == "LIMIT" and o["side"] == exit_side), None)
    except Exception as exc:
        logger.warning("{} Could not fetch open orders during recovery: {}", symbol, exc)
    stop_ids = [o["order_id"] for o in [sl_limit_o, sl_market_o] if o is not None]
    sl_limit_price = sl_limit_o["stop_price"] if sl_limit_o else Decimal("0")
    sl_market_price = sl_market_o["stop_price"] if sl_market_o else Decimal("0")
    has_details = bool(stop_ids or ttp_o or tp_limit_o)
    trade_manager.register_trade(
        symbol=symbol,
        position=pos,
        size=abs(pos_data["amount"]),
        entry_price=pos_data["entry_price"],
        tick_size=sym_info["tick_size"],
        stop_ids=stop_ids,
        sl_limit_price=sl_limit_price,
        sl_market_price=sl_market_price,
        ttp_id=ttp_o["order_id"] if ttp_o else None,
        tp_limit_id=tp_limit_o["order_id"] if tp_limit_o else None,
        has_order_details=has_details,
    )
    if has_details:
        logger.info(
            "{} Recovered {} position registered — found orders: sl_limit={} sl_market={} ttp={} tp_limit={}.",
            symbol, pos.value,
            sl_limit_o["order_id"] if sl_limit_o else None,
            sl_market_o["order_id"] if sl_market_o else None,
            ttp_o["order_id"] if ttp_o else None,
            tp_limit_o["order_id"] if tp_limit_o else None,
        )
    else:
        logger.warning("{} Recovered {} position registered — exit order IDs unknown.", symbol, pos.value)


def _load_risk_params(cfg: dict) -> dict:
    """Extract order-execution risk percentages from config into a flat dict of Decimal values."""
    risk = cfg["risk"]
    entry = cfg.get("entry", {})
    return {
        "sl_limit_pct":          Decimal(str(risk.get("stop_loss_limit_pct", 1.0))) / 100,
        "sl_market_pct":         Decimal(str(risk.get("stop_loss_market_pct", 2.0))) / 100,
        "tp_limit_pct":          Decimal(str(risk.get("take_profit_limit_pct", 3.0))) / 100,
        "ttp_activation_pct":    Decimal(str(risk.get("trailing_take_profit_activation_pct", 1.0))) / 100,
        "ttp_callback_rate":     Decimal(str(risk.get("trailing_take_profit_callback_rate", 2.0))),
        "max_entry_deviation_pct": Decimal(str(entry.get("max_price_deviation_pct", 0.3))) / 100,
    }


def _build_trade_manager(client, cfg: dict) -> TradeManager:
    """Construct a TradeManager from config."""
    risk = cfg["risk"]
    tm = cfg.get("trade_manager", {})
    return TradeManager(
        client,
        poll_interval_secs=int(tm.get("poll_interval_secs", 10)),
        sl_profit_trigger_pct=Decimal(str(risk.get("sl_profit_trigger_pct", 1.0))) / 100,
        sl_profit_lock_pct=Decimal(str(risk.get("sl_profit_lock_pct", 0.5))) / 100,
        sl_profit_market_lock_pct=Decimal(str(risk.get("sl_profit_market_lock_pct", 0.3))) / 100,
        min_residual_notional=Decimal(str(tm.get("min_residual_notional_usdt", "10"))),
    )


def _prefetch_candles(client, symbols: list[str], interval: str, cfg: dict) -> tuple[int, dict[str, list[dict]]]:
    """Pre-fetch REST candle history for indicator warmup. Returns (candle_limit, buffers)."""
    _cfg_limit = cfg["trading"].get("candle_limit")
    if _cfg_limit:
        candle_limit = int(_cfg_limit)
    else:
        interval_min = interval_to_minutes(interval)
        candle_limit = (200 * 60 // interval_min) + 50
    logger.info("Candle limit: {} (interval={})", candle_limit, interval)
    buffers: dict[str, list[dict]] = {}
    for symbol in symbols:
        buffers[symbol] = market.get_futures_ohlcv(client, symbol, interval, limit=candle_limit)
        logger.info("Prefetched {} candles for {}", len(buffers[symbol]), symbol)
    return candle_limit, buffers


def _update_buffer(buf: list[dict], candle: dict, candle_limit: int) -> None:
    """Append a closed candle to the buffer, replacing an open candle for the same period if present."""
    if buf and candle["open_time"] == buf[-1]["open_time"]:
        buf[-1] = candle
    else:
        buf.append(candle)
        if len(buf) > candle_limit:
            buf.pop(0)


# ---------------------------------------------------------------------------
# IOC entry
# ---------------------------------------------------------------------------

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

        time.sleep(0.1)
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


# ---------------------------------------------------------------------------
# Signal execution
# ---------------------------------------------------------------------------

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
        pos_utils.close_position(client, symbol)
        trade_manager.close_trade(symbol)


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
# Per-tick processing
# ---------------------------------------------------------------------------

def _check_stagnation(
    symbol: str,
    buf: list[dict],
    signal: TradeSignal,
    strategy_params: dict,
    trade_manager: TradeManager,
) -> TradeSignal | None:
    """Run the stagnation/reversal check on a HOLD candle.

    Uses indicator values carried on the signal to avoid recomputing them.
    Returns a CLOSE TradeSignal (suppress_reentry=True) if stagnation is confirmed,
    or None to keep holding.
    """
    triggered = trade_manager.tick_stagnation(
        symbol=symbol,
        current_price=buf[-1]["close"],
        current_adx=signal.current_adx if signal.current_adx is not None else 0.0,
        current_rsi=signal.current_rsi if signal.current_rsi is not None else 50.0,
        min_adx=float(strategy_params.get("min_adx", 25.0)),
        rsi_long_low=float(strategy_params.get("rsi_long_low", 50.0)),
        rsi_short_high=float(strategy_params.get("rsi_short_high", 50.0)),
        stagnation_candles=int(strategy_params.get("stagnation_candles", 4)),
        stagnation_min_pct=float(strategy_params.get("stagnation_min_pct", 2.0)),
        stagnation_reversal_pct=float(strategy_params.get("stagnation_reversal_pct", 0.15)),
    )
    if triggered:
        logger.info("{} Stagnation exit triggered — overriding HOLD to CLOSE.", symbol)
        return TradeSignal(signal=Signal.CLOSE, symbol=symbol,
                           reason="stagnation exit — momentum decayed", suppress_reentry=True)
    return None


def _process_symbol_tick(
    symbol: str,
    buf: list[dict],
    strategy_fn,
    strategy_params: dict,
    trade_manager: TradeManager,
    risk: "RiskGuard",
    execute_fn,
) -> None:
    """Evaluate one closed candle for a symbol: run strategy, check stagnation, execute signal."""
    position = trade_manager.get_position(symbol)
    signal = strategy_fn(buf, symbol, position, strategy_params)
    logger.info("{} [{}] {} — {}", symbol, position.value, signal.signal.value, signal.reason)

    if signal.signal == Signal.HOLD and position != Position.NONE:
        stagnation = _check_stagnation(symbol, buf, signal, strategy_params, trade_manager)
        if stagnation:
            signal = stagnation

    if signal.signal == Signal.HOLD or not risk.check():
        return

    execute_fn(symbol, signal)

    # Immediately re-evaluate on the same candle after a close.
    # Handles trend reversals (close short → open long) and RSI flush re-entries
    # without waiting for the next candle. Skipped after a stagnation close — the
    # same price data that triggered the exit would immediately re-enter.
    if (
        signal.signal == Signal.CLOSE
        and not signal.suppress_reentry
        and trade_manager.get_position(symbol) == Position.NONE
        and risk.check()
    ):
        reentry = strategy_fn(buf, symbol, Position.NONE, strategy_params)
        logger.info("{} [NONE] {} — {} (re-entry check)", symbol, reentry.signal.value, reentry.reason)
        if reentry.signal in (Signal.OPEN_LONG, Signal.OPEN_SHORT):
            execute_fn(symbol, reentry)


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

    risk_params = _load_risk_params(cfg)
    sym_info = setup_symbols(client, symbols, cfg["risk"]["leverage"])

    trade_manager = _build_trade_manager(client, cfg)
    trade_manager.start()

    pnl_reporter = DailyPnLReporter(client, symbols, cfg["reporting"]["pnl_csv_file"])
    pnl_reporter.start()

    # Register any positions already open on Binance so TradeManager can detect
    # external closes between candles. Query open orders to recover exit order IDs.
    recovered = recover_positions(client, symbols)
    for symbol, pos_data in recovered.items():
        if pos_data is not None:
            _register_recovered_position(
                client, symbol, Position[pos_data["side"]], pos_data, sym_info[symbol], trade_manager
            )

    candle_limit, candle_buffers = _prefetch_candles(client, symbols, interval, cfg)

    event_queue: queue.SimpleQueue = queue.SimpleQueue()

    def on_closed_candle(sym: str, candle: dict) -> None:
        event_queue.put((sym, candle))

    twm = market.start_kline_streams(
        env["api_key"], env["api_secret"], env["testnet"],
        symbols, interval, on_closed_candle,
    )

    def _execute(sym: str, sig: TradeSignal) -> None:
        execute_signal(
            client, sym, sig, risk.max_position_usdt, sym_info[sym], trade_manager,
            **risk_params,
        )

    try:
        while True:
            symbol, candle = event_queue.get()
            _update_buffer(candle_buffers[symbol], candle, candle_limit)
            try:
                _process_symbol_tick(
                    symbol, candle_buffers[symbol],
                    strategy_fn, strategy_params,
                    trade_manager, risk, _execute,
                )
            except Exception as exc:
                logger.exception("Error processing {}: {}", symbol, exc)
    finally:
        trade_manager.stop()
        twm.stop()
        logger.info("WebSocket streams stopped.")


if __name__ == "__main__":
    run()
