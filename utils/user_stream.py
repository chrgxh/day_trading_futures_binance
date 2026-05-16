"""Authenticated Binance Futures user-data WebSocket stream.

Maintains a listenKey (created on connect, kept alive every 30 minutes), connects
to the futures user-data stream, and delivers raw account/order events to a
callback. Reconnects automatically on disconnect; recreates the listenKey on
every reconnect and forces a reconnect when Binance signals `listenKeyExpired`.

Events delivered (raw dicts, see Binance docs):
  - ACCOUNT_UPDATE        — balance / position changes
  - ORDER_TRADE_UPDATE    — order NEW / FILLED / PARTIALLY_FILLED / CANCELED ...
  - ACCOUNT_CONFIG_UPDATE — leverage changes
The consumer decides which event types it cares about.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Callable

import websockets
from binance.client import Client
from loguru import logger

_FSTREAM_URL = "wss://fstream.binance.com"
_FSTREAM_TESTNET_URL = "wss://stream.binancefuture.com"
_KEEPALIVE_SECS = 30 * 60  # Binance expires an idle listenKey after 60 min.


class UserDataStream:
    """Direct WebSocket connection to the Binance Futures user-data stream.

    Args:
        client: Authenticated Binance client (used for listenKey REST calls).
        testnet: If True, connects to the futures testnet WebSocket endpoint.
        on_event: Called with each raw event dict. Invoked from a background
            thread — route shared state through a queue.
        on_connect: Called with no args after every (re)connect, so the consumer
            can REST-resync its state to cover any events missed while down.
    """

    def __init__(
        self,
        client: Client,
        testnet: bool,
        on_event: Callable[[dict], None],
        on_connect: Callable[[], None],
    ) -> None:
        self._client = client
        self._testnet = testnet
        self._on_event = on_event
        self._on_connect = on_connect
        self._listen_key: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="user-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._listen_key is not None:
            try:
                self._client.futures_stream_close(self._listen_key)
            except Exception:
                pass

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._stream_loop())
        except Exception as exc:
            if not self._stop.is_set():
                logger.error("[user-ws] thread error: {}", exc)
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()

    async def _stream_loop(self) -> None:
        base = _FSTREAM_TESTNET_URL if self._testnet else _FSTREAM_URL
        assert self._loop is not None

        while not self._stop.is_set():
            keepalive_task: asyncio.Task | None = None
            try:
                self._listen_key = await self._loop.run_in_executor(
                    None, self._client.futures_stream_get_listen_key
                )
                url = f"{base}/ws/{self._listen_key}"
                async with websockets.connect(url, ping_interval=20, ping_timeout=60) as ws:
                    logger.info("[user-ws] connected")
                    keepalive_task = asyncio.ensure_future(self._keepalive())
                    self._on_connect()
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        try:
                            reconnect = self._handle_message(raw)
                        except Exception as exc:
                            logger.error("[user-ws] handler error: {}", exc)
                            continue
                        if reconnect:
                            break  # exit async-for -> recreate listenKey + reconnect
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._stop.is_set():
                    return
                logger.warning("[user-ws] disconnected ({}), reconnecting in 5s", exc)
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    return
            finally:
                if keepalive_task is not None:
                    keepalive_task.cancel()

    async def _keepalive(self) -> None:
        assert self._loop is not None
        try:
            while not self._stop.is_set():
                await asyncio.sleep(_KEEPALIVE_SECS)
                try:
                    await self._loop.run_in_executor(
                        None, self._client.futures_stream_keepalive, self._listen_key
                    )
                    logger.debug("[user-ws] listenKey kept alive")
                except Exception as exc:
                    logger.warning("[user-ws] keepalive failed: {}", exc)
        except asyncio.CancelledError:
            return

    def _handle_message(self, raw: str) -> bool:
        """Parse one WS frame. Returns True if the connection must be recycled."""
        msg = json.loads(raw)
        event = msg.get("e")
        if event == "listenKeyExpired":
            logger.warning("[user-ws] listenKey expired — recycling connection")
            return True
        if event:
            self._on_event(msg)
        return False
