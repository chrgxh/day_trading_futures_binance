"""Integration tests for the live WebSocket streams against Binance testnet.

Two streams are exercised end-to-end:
  - the public kline (candle) stream via `market.start_kline_streams`
  - the authenticated user-data stream via `user_stream.UserDataStream`

Both are slow by nature — the kline test waits for a real 1m candle to close,
the user-data test waits for an order event to round-trip through Binance.
"""

import threading
import time
from decimal import Decimal

import pytest

from utils import market
from utils import account as account_mod
from utils import orders as orders_mod
from utils import positions as positions_mod
from utils.user_stream import UserDataStream

pytestmark = pytest.mark.integration


def test_kline_websocket_delivers_closed_candle(client, symbol):
    """The kline WS should deliver at least one closed 1m candle.

    Waits up to ~140s — enough to cross at least one 1m boundary regardless of
    where in the current minute the test starts.
    """
    received: list[tuple[str, str, dict]] = []
    got_candle = threading.Event()

    def on_closed_candle(sym: str, interval: str, candle: dict) -> None:
        received.append((sym, interval, candle))
        got_candle.set()

    mgr = market.start_kline_streams(
        client=client,
        testnet=True,
        pairs=[(symbol, "1m")],
        on_closed_candle=on_closed_candle,
    )
    try:
        assert mgr._twm.is_alive(), "socket manager thread should be running"
        assert got_candle.wait(timeout=140), "no closed candle delivered within 140s"
    finally:
        mgr.stop()

    sym, interval, candle = received[0]
    assert sym == symbol
    assert interval == "1m"
    assert {"open_time", "open", "high", "low", "close", "volume", "close_time"} <= candle.keys()
    assert isinstance(candle["close"], Decimal)
    assert candle["high"] >= candle["low"]
    assert candle["close_time"] > candle["open_time"]


def test_user_data_websocket_delivers_order_event(client, symbol, sym_info):
    """The user-data WS should deliver an ORDER_TRADE_UPDATE for our symbol.

    Starts the stream, then places and closes a minimal market position; the
    resulting order activity must round-trip back over the socket.
    """
    events: list[dict] = []
    got_order_event = threading.Event()
    lock = threading.Lock()

    def on_event(msg: dict) -> None:
        with lock:
            events.append(msg)
        if msg.get("e") == "ORDER_TRADE_UPDATE" and msg.get("o", {}).get("s") == symbol:
            got_order_event.set()

    def on_connect() -> None:
        pass

    # Clean slate so the only order activity is ours.
    orders_mod.cancel_all_orders(client, symbol)
    positions_mod.close_position(client, symbol)

    stream = UserDataStream(client, testnet=True, on_event=on_event, on_connect=on_connect)
    stream.start()
    try:
        # Give the socket a moment to connect before generating events.
        time.sleep(5)

        qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])
        orders_mod.place_market_order(client, symbol, "BUY", qty)

        assert got_order_event.wait(timeout=30), "no ORDER_TRADE_UPDATE delivered within 30s"
    finally:
        stream.stop()
        try:
            orders_mod.cancel_all_orders(client, symbol)
        except Exception:
            pass
        try:
            positions_mod.close_position(client, symbol)
        except Exception:
            pass

    with lock:
        order_events = [
            e for e in events
            if e.get("e") == "ORDER_TRADE_UPDATE" and e.get("o", {}).get("s") == symbol
        ]
    assert order_events, "expected at least one ORDER_TRADE_UPDATE for our symbol"
    o = order_events[0]["o"]
    assert o["s"] == symbol
    assert o["S"] in ("BUY", "SELL")
    assert o["X"] in ("NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED")
