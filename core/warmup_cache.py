"""Disk cache for warmup candles.

The bot seeds each strategy's per-(symbol, interval) candle buffer from REST
history at startup. A cold start fetches every buffer in full — a burst of
weighted kline calls that, on the CloudFront-fronted testnet, shares a
rate-limit bucket with every other client on the same edge POP and reliably
trips Binance's -1003 IP ban regardless of the bot's own request volume.

`WarmupCache` persists those buffers to a single JSON file on the `state/`
volume. A restart re-fetches only the candles that closed while the bot was
down — often a handful, or none. The object owns the whole lifecycle:

  - `get_warmup(...)` — read a buffer from the cache, or REST-fetch (and gap-fill)
    what is missing. The bot calls only this; no cache-vs-fetch logic leaks out.
  - `update(...)`   — fold a newly-closed live candle into the in-memory buffer,
    trimming the oldest so it stays a fixed-size rolling window.
  - `save()`        — atomically persist every buffer to disk.

Constructed with `path=None` the cache is disabled: `get_warmup` always does a
full fetch and `update`/`save` are no-ops.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

from binance.client import Client
from loguru import logger

from utils import market
from utils.indicators import interval_to_minutes

_VERSION = 1

# OHLCV fields are Decimal in the candle dict; JSON has no Decimal, so they are
# stored as strings and parsed back. open_time / close_time are plain ints.
_DECIMAL_FIELDS = ("open", "high", "low", "close", "volume")


def _candle_to_json(c: dict) -> dict:
    d: dict = {"open_time": c["open_time"], "close_time": c["close_time"]}
    for f in _DECIMAL_FIELDS:
        d[f] = str(c[f])
    return d


def _candle_from_json(d: dict) -> dict:
    c: dict = {"open_time": int(d["open_time"]), "close_time": int(d["close_time"])}
    for f in _DECIMAL_FIELDS:
        c[f] = Decimal(d[f])
    return c


def _merge(a: list[dict], b: list[dict]) -> list[dict]:
    """Merge two candle lists, de-duplicated by open_time, sorted ascending."""
    by_time: dict[int, dict] = {c["open_time"]: c for c in a}
    by_time.update({c["open_time"]: c for c in b})
    return [by_time[t] for t in sorted(by_time)]


class WarmupCache:
    """Single-file disk cache of warmup candle buffers, keyed by (symbol, interval).

    The JSON file holds every buffer under string keys ``"<SYMBOL>|<interval>"``;
    it is overwritten in place and each buffer is capped at the strategy's
    `candle_limit`, so the file is a fixed-size rolling window that never grows.
    """

    def __init__(self, path: str | None) -> None:
        """Load the cache file, or start empty.

        Args:
            path: JSON cache file path, or None to disable persistence.
        """
        self._path = path
        self._buffers: dict[str, list[dict]] = {}
        # Per-key candle_limit, learned from get_warmup and used by update to
        # trim. In-memory only — not persisted, re-learned every startup.
        self._limits: dict[str, int] = {}
        if path and os.path.exists(path):
            self._load()

    @staticmethod
    def _key(symbol: str, interval: str) -> str:
        return f"{symbol}|{interval}"

    def _load(self) -> None:
        """Read the cache file into memory; on any error start empty."""
        try:
            with open(self._path) as fh:
                raw = json.load(fh)
            if raw.get("version") != _VERSION:
                logger.warning("[warmup-cache] version mismatch in {} — starting empty", self._path)
                return
            self._buffers = {
                key: [_candle_from_json(c) for c in candles]
                for key, candles in raw.get("buffers", {}).items()
            }
            logger.info("[warmup-cache] loaded {} buffer(s) from {}",
                        len(self._buffers), self._path)
        except Exception as exc:
            logger.warning("[warmup-cache] could not read {}: {} — starting empty",
                           self._path, exc)
            self._buffers = {}

    def get_warmup(
        self, client: Client, symbol: str, interval: str, limit: int,
    ) -> list[dict]:
        """Return up to `limit` closed warmup candles for (symbol, interval).

        Serves the buffer from the cache when possible, REST-fetching only the
        candles that closed since it was last written. Falls back to a full
        fetch when there is no usable cache, when it is too stale, or when it
        holds fewer than `limit` candles. The bot calls only this — the
        cache-vs-fetch decision lives entirely here.

        Args:
            client: Authenticated Binance client.
            symbol: Trading pair.
            interval: Kline interval.
            limit: Number of closed candles the strategy buffer needs.

        Returns:
            Closed candle dicts, oldest first, length up to `limit`.
        """
        key = self._key(symbol, interval)
        self._limits[key] = limit

        # Fetch one extra: REST klines include the still-forming candle, removed
        # by market.drop_forming_candle (the kline WS delivers only closed
        # candles, so a forming candle seeded into a buffer would stay stale).
        def _full_fetch() -> list[dict]:
            candles = market.get_futures_ohlcv(client, symbol, interval, limit=limit + 1)
            return market.drop_forming_candle(candles)

        if self._path is None:
            return _full_fetch()

        cached = self._buffers.get(key, [])
        if len(cached) < limit:
            candles = _full_fetch()
            self._buffers[key] = candles
            return candles

        interval_ms = interval_to_minutes(interval) * 60_000
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # Candle-starts since the last cached candle opened. The most recent one
        # is still forming; any before it have closed and need fetching.
        elapsed = (now_ms - cached[-1]["open_time"]) // interval_ms

        if elapsed <= 1:
            # Only a forming candle has opened since — the cache is current.
            logger.info("[warmup-cache] {} {} hit: {} candles, 0 fetched",
                        symbol, interval, len(cached))
            self._buffers[key] = cached[-limit:]
            return self._buffers[key]

        if elapsed >= limit:
            # Down longer than the whole buffer — a full fetch is simpler and no
            # more expensive than a gap fetch of that size.
            candles = _full_fetch()
            self._buffers[key] = candles
            return candles

        # Gap fill: fetch only what closed since the cache was written (+overlap
        # so the de-dup in _merge has something to anchor on).
        fetch = min(elapsed + 2, limit + 1)
        fresh = market.drop_forming_candle(
            market.get_futures_ohlcv(client, symbol, interval, limit=fetch)
        )
        merged = _merge(cached, fresh)[-limit:]
        logger.info("[warmup-cache] {} {} gap-fill: {} cached + {} fetched -> {}",
                    symbol, interval, len(cached), len(fresh), len(merged))
        self._buffers[key] = merged
        return merged

    def update(self, symbol: str, interval: str, candle: dict) -> None:
        """Fold a newly-closed live candle into the in-memory buffer.

        Appends the candle (or replaces the last one if re-delivered with the
        same open_time) and trims the oldest candles past `candle_limit`. Memory
        only — persistence happens on `save()`.
        """
        if self._path is None:
            return
        key = self._key(symbol, interval)
        buf = self._buffers.setdefault(key, [])
        if buf and candle["open_time"] == buf[-1]["open_time"]:
            buf[-1] = candle
        else:
            buf.append(candle)
        limit = self._limits.get(key)
        if limit is not None and len(buf) > limit:
            del buf[: len(buf) - limit]

    def save(self) -> None:
        """Atomically persist every buffer to the cache file.

        Write-to-temp-then-rename so a crash mid-write never leaves a partial
        file. Failures are logged and swallowed — the cache is an optimisation,
        not a correctness dependency. No-op when the cache is disabled.
        """
        if self._path is None:
            return
        payload = {
            "version": _VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "buffers": {
                key: [_candle_to_json(c) for c in candles]
                for key, candles in self._buffers.items()
            },
        }
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self._path)
            logger.info("[warmup-cache] saved {} buffer(s) to {}",
                        len(self._buffers), self._path)
        except Exception as exc:
            logger.warning("[warmup-cache] could not save {}: {}", self._path, exc)
