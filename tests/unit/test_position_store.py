"""Unit tests for PositionStore — JSON I/O only, no business logic."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.position_store import SCHEMA_VERSION, PositionStore


def test_load_missing_file_returns_empty(tmp_path: Path):
    store = PositionStore(tmp_path / "positions.json")
    assert store.load() == {}
    assert store.all() == {}


def test_upsert_and_save_roundtrip(tmp_path: Path):
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    store.upsert(
        "BTCUSDT",
        strategy="adaptive_trend_pullback",
        side="LONG",
        entry_price=Decimal("30000.5"),
        qty=Decimal("0.015"),
        strategy_state={"entry_atr": 412.3, "r_distance": 825.0},
        orders={"stop_loss_id": 12345, "tp1_id": 12346},
    )
    store.save()

    fresh = PositionStore(path)
    fresh.load()
    entry = fresh.get("BTCUSDT")
    assert entry is not None
    assert entry["strategy"] == "adaptive_trend_pullback"
    assert entry["side"] == "LONG"
    assert entry["entry_price"] == "30000.5"
    assert entry["qty"] == "0.015"
    assert entry["strategy_state"]["entry_atr"] == 412.3
    assert entry["orders"]["stop_loss_id"] == 12345


def test_save_is_atomic_rename(tmp_path: Path):
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="1", qty="1", strategy_state={}, orders={})
    store.save()
    # No leftover .tmp files in the directory
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_save_writes_schema_version(tmp_path: Path):
    path = tmp_path / "positions.json"
    store = PositionStore(path)
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="1", qty="1", strategy_state={}, orders={})
    store.save()
    raw = json.loads(path.read_text())
    assert raw["version"] == SCHEMA_VERSION
    assert "updated_at" in raw
    assert "BTCUSDT" in raw["positions"]


def test_patch_updates_subset(tmp_path: Path):
    store = PositionStore(tmp_path / "p.json")
    store.upsert("ETHUSDT", strategy="s", side="LONG",
                 entry_price="2000", qty="0.5",
                 strategy_state={"v": 1}, orders={"stop_loss_id": 1})
    store.patch("ETHUSDT", strategy_state={"v": 2})
    entry = store.get("ETHUSDT")
    assert entry["strategy_state"] == {"v": 2}
    assert entry["orders"] == {"stop_loss_id": 1}  # unchanged

    store.patch("ETHUSDT", orders={"stop_loss_id": 99}, qty="0.7")
    entry = store.get("ETHUSDT")
    assert entry["orders"] == {"stop_loss_id": 99}
    assert entry["qty"] == "0.7"


def test_patch_unknown_symbol_is_noop(tmp_path: Path):
    store = PositionStore(tmp_path / "p.json")
    store.patch("UNKNOWN", strategy_state={"x": 1})  # no error
    assert store.get("UNKNOWN") is None


def test_remove(tmp_path: Path):
    store = PositionStore(tmp_path / "p.json")
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="1", qty="1", strategy_state={}, orders={})
    assert store.remove("BTCUSDT") is True
    assert store.get("BTCUSDT") is None
    assert store.remove("BTCUSDT") is False


def test_upsert_preserves_opened_at_across_calls(tmp_path: Path):
    store = PositionStore(tmp_path / "p.json")
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="1", qty="1", strategy_state={}, orders={})
    first_opened = store.get("BTCUSDT")["opened_at"]
    # Re-upsert (e.g. after restart) — opened_at should be preserved.
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="2", qty="2", strategy_state={"x": 1}, orders={})
    assert store.get("BTCUSDT")["opened_at"] == first_opened
    assert store.get("BTCUSDT")["entry_price"] == "2"


def test_load_quarantines_corrupt_file(tmp_path: Path):
    path = tmp_path / "p.json"
    path.write_text("{ not json")
    store = PositionStore(path)
    assert store.load() == {}
    # Original file moved aside.
    assert not path.exists()
    quarantined = list(tmp_path.glob("p.json.corrupt-*"))
    assert len(quarantined) == 1


def test_load_rejects_unknown_schema_version(tmp_path: Path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"version": 99, "positions": {"X": {"strategy": "s"}}}))
    store = PositionStore(path)
    assert store.load() == {}


def test_get_returns_copy(tmp_path: Path):
    store = PositionStore(tmp_path / "p.json")
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="1", qty="1",
                 strategy_state={"v": 1}, orders={})
    entry = store.get("BTCUSDT")
    entry["strategy_state"]["v"] = 999
    # Internal state must not be mutated by external edits.
    assert store.get("BTCUSDT")["strategy_state"]["v"] == 1


def test_upsert_defaults_status_to_open(tmp_path: Path):
    store = PositionStore(tmp_path / "p.json")
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="1", qty="1", strategy_state={}, orders={})
    assert store.get("BTCUSDT")["status"] == "open"


def test_upsert_pending_status_roundtrip(tmp_path: Path):
    path = tmp_path / "p.json"
    store = PositionStore(path)
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="30000", qty="0.1", strategy_state={},
                 orders={"entry_id": 555}, status="pending")
    store.save()

    fresh = PositionStore(path)
    fresh.load()
    entry = fresh.get("BTCUSDT")
    assert entry["status"] == "pending"
    assert entry["orders"]["entry_id"] == 555


def test_patch_updates_status(tmp_path: Path):
    store = PositionStore(tmp_path / "p.json")
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="1", qty="1", strategy_state={}, orders={},
                 status="pending")
    store.patch("BTCUSDT", status="open")
    assert store.get("BTCUSDT")["status"] == "open"


def test_patch_updates_side_and_entry_price(tmp_path: Path):
    store = PositionStore(tmp_path / "p.json")
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="29950", qty="0.2", strategy_state={},
                 orders={"entry_id": 1}, status="pending")
    store.patch("BTCUSDT", status="open", side="LONG",
                entry_price=Decimal("30000.5"), qty="0.1")
    entry = store.get("BTCUSDT")
    assert entry["status"] == "open"
    assert entry["side"] == "LONG"
    assert entry["entry_price"] == "30000.5"
    assert entry["qty"] == "0.1"


def test_load_defaults_missing_status_to_open(tmp_path: Path):
    # Entries written before `status` existed must still load (as "open").
    path = tmp_path / "p.json"
    path.write_text(json.dumps({
        "version": SCHEMA_VERSION, "updated_at": "...",
        "positions": {
            "BTCUSDT": {
                "strategy": "s", "opened_at": "...", "side": "LONG",
                "entry_price": "1", "qty": "1", "strategy_state": {}, "orders": {},
            }
        }
    }))
    store = PositionStore(path)
    store.load()
    assert store.get("BTCUSDT")["status"] == "open"


def test_save_creates_parent_directory(tmp_path: Path):
    nested = tmp_path / "deeply" / "nested" / "positions.json"
    store = PositionStore(nested)
    store.upsert("BTCUSDT", strategy="s", side="LONG",
                 entry_price="1", qty="1", strategy_state={}, orders={})
    store.save()
    assert nested.exists()
