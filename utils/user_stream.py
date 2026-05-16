"""Authenticated Binance Futures user-data WebSocket stream.

Wraps python-binance's `ThreadedWebsocketManager`, which owns the socket URL
(testnet vs mainnet), its own thread + event loop, the listenKey lifecycle
(creation, 30-min keepalive, recreation on expiry), and reconnection. This
module only filters the raw event stream and forwards it to a callback.

Events delivered (raw dicts, see Binance docs):
  - ACCOUNT_UPDATE        — balance / position changes
  - ORDER_TRADE_UPDATE    — order NEW / FILLED / PARTIALLY_FILLED / CANCELED ...
  - ACCOUNT_CONFIG_UPDATE — leverage changes
The consumer decides which event types it cares about.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from binance import ThreadedWebsocketManager
from binance.client import Client
from loguru import logger


class UserDataStream:
    """Binance Futures user-data stream over the python-binance socket manager.

    Args:
        client: Authenticated Binance client (its API key/secret are reused to
            build the socket manager).
        testnet: If True, the socket manager connects to the futures testnet
            endpoints.
        on_event: Called with each raw event dict. Invoked from the socket
            manager's background thread — route shared state through a queue.
        on_connect: Called with no args whenever the socket manager reports a
            disconnect, so the consumer can REST-resync its state to cover any
            events missed while the socket was down.
    """

    def __init__(
        self,
        client: Client,
        testnet: bool,
        on_event: Callable[[dict], None],
        on_connect: Callable[[], None],
    ) -> None:
        self._on_event = on_event
        self._on_connect = on_connect
        # A dedicated loop per manager: ThreadedWebsocketManager otherwise
        # defaults to asyncio.get_event_loop(), which hands every manager
        # constructed on the main thread the *same* loop — the second one to
        # start then fails ("Socket Manager failed to initialize").
        self._twm = ThreadedWebsocketManager(
            api_key=client.API_KEY,
            api_secret=client.API_SECRET,
            testnet=testnet,
            loop=asyncio.new_event_loop(),
        )

    def start(self) -> None:
        self._twm.start()
        self._twm.start_futures_user_socket(callback=self._handle_message)
        logger.info("[user-ws] started")

    def stop(self) -> None:
        try:
            self._twm.stop()
        except Exception as exc:
            logger.warning("[user-ws] stop failed: {}", exc)

    def _handle_message(self, msg: dict) -> None:
        """Handle one user-data event dict from ThreadedWebsocketManager.

        The socket manager emits {"e": "error", ...} on disconnect; it reconnects
        and recreates the listenKey on its own, so we only trigger a resync to
        cover events missed while down. `listenKeyExpired` is handled by the SDK
        too — we resync for the same reason.
        """
        try:
            event = msg.get("e")
            if event == "error":
                logger.warning("[user-ws] stream error ({}) — resyncing", msg.get("m"))
                self._on_connect()
                return
            if event == "listenKeyExpired":
                logger.warning("[user-ws] listenKey expired (SDK recreating) — resyncing")
                self._on_connect()
                return
            if event:
                self._on_event(msg)
        except Exception as exc:
            logger.error("[user-ws] handler error: {}", exc)
