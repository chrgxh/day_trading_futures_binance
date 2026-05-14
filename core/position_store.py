"""Persistent store mapping live positions to the strategy that owns them.

This is a cache. Binance is always the source of truth. On restart, StateManager
loads this file, drops entries whose positions no longer exist, and lets strategies
adopt any positions they previously owned. The file is rewritten on every poll
using a write-to-temp-then-rename pattern so it's safe across crashes.

Schema (versioned for future migrations):

    {
      "version": 1,
      "updated_at": "<ISO-8601 UTC>",
      "positions": {
        "<SYMBOL>": {
          "strategy": "<strategy_name>",
          "opened_at": "<ISO-8601 UTC>",
          "side": "LONG" | "SHORT",
          "entry_price": "<decimal string>",
          "qty": "<decimal string>",
          "strategy_state": { ... },   # opaque, owned by strategy
          "orders": { "<role>": <order_id>, ... }
        },
        ...
      }
    }
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


SCHEMA_VERSION = 1


class PositionStore:
    """Thread-unsafe JSON store. Callers must serialize access (StateManager does)."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._entries: dict[str, dict[str, Any]] = {}

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def load(self) -> dict[str, dict[str, Any]]:
        """Load entries from disk. Returns the in-memory map (also stored internally).

        Missing file is fine — starts empty. Corrupt file is logged and quarantined
        (renamed to <path>.corrupt-<ts>) so subsequent runs start clean.
        """
        if not self._path.exists():
            self._entries = {}
            return self._entries
        try:
            with self._path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            quarantine = self._path.with_suffix(self._path.suffix + f".corrupt-{ts}")
            try:
                self._path.rename(quarantine)
            except OSError:
                pass
            logger.error(
                "[positions] could not read {}: {} — quarantined to {}",
                self._path, exc, quarantine,
            )
            self._entries = {}
            return self._entries

        version = raw.get("version")
        if version != SCHEMA_VERSION:
            logger.warning(
                "[positions] schema version mismatch ({} vs expected {}) — ignoring file",
                version, SCHEMA_VERSION,
            )
            self._entries = {}
            return self._entries

        positions = raw.get("positions") or {}
        if not isinstance(positions, dict):
            logger.warning("[positions] malformed file (positions not a dict) — ignoring")
            self._entries = {}
            return self._entries

        self._entries = {str(k): dict(v) for k, v in positions.items() if isinstance(v, dict)}
        logger.info("[positions] loaded {} entry(s) from {}", len(self._entries), self._path)
        return self._entries

    def save(self) -> None:
        """Atomic write: serialize to a temp file in the same directory, then rename."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "positions": self._entries,
        }
        fd, tmp_path = tempfile.mkstemp(
            prefix=self._path.name + ".",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.error("[positions] could not write {}: {}", self._path, exc)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get(self, symbol: str) -> dict[str, Any] | None:
        entry = self._entries.get(symbol)
        return copy.deepcopy(entry) if entry is not None else None

    def all(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._entries)

    # ------------------------------------------------------------------
    # Write API — caller must save() to persist
    # ------------------------------------------------------------------

    def upsert(
        self,
        symbol: str,
        *,
        strategy: str,
        side: str,
        entry_price: Any,
        qty: Any,
        strategy_state: dict[str, Any],
        orders: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        existing = self._entries.get(symbol, {})
        opened_at = existing.get("opened_at", now)
        self._entries[symbol] = {
            "strategy": strategy,
            "opened_at": opened_at,
            "side": side,
            "entry_price": str(entry_price),
            "qty": str(qty),
            "strategy_state": dict(strategy_state),
            "orders": dict(orders),
        }

    def patch(
        self,
        symbol: str,
        *,
        strategy_state: dict[str, Any] | None = None,
        orders: dict[str, Any] | None = None,
        qty: Any | None = None,
    ) -> None:
        """Update a subset of fields for an existing entry. No-op if symbol absent."""
        entry = self._entries.get(symbol)
        if entry is None:
            return
        if strategy_state is not None:
            entry["strategy_state"] = dict(strategy_state)
        if orders is not None:
            entry["orders"] = dict(orders)
        if qty is not None:
            entry["qty"] = str(qty)

    def remove(self, symbol: str) -> bool:
        return self._entries.pop(symbol, None) is not None
