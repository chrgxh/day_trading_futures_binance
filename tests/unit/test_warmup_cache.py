"""Unit tests for core.warmup_cache.WarmupCache — no network (market mocked)."""

from datetime import datetime, timezone
from decimal import Decimal

from core.warmup_cache import WarmupCache, _merge

_INTERVAL = "4h"
_INTERVAL_MS = 240 * 60_000


def _candle(open_time: int, price: str = "100") -> dict:
    return {
        "open_time": open_time,
        "close_time": open_time + _INTERVAL_MS - 1,
        "open": Decimal(price),
        "high": Decimal(price),
        "low": Decimal(price),
        "close": Decimal(price),
        "volume": Decimal("1.5"),
    }


def _series(end_open: int, count: int) -> list[dict]:
    """`count` consecutive candles, the last opening at `end_open`."""
    return [_candle(end_open - (count - 1 - i) * _INTERVAL_MS) for i in range(count)]


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _mock_market(monkeypatch, candles: list[dict], calls: list | None = None) -> None:
    """Point warmup_cache.market.get_futures_ohlcv at a fixed candle list."""
    import core.warmup_cache as wc

    def _get(c, s, i, limit):
        if calls is not None:
            calls.append(limit)
        return candles

    monkeypatch.setattr(wc.market, "get_futures_ohlcv", _get)
    monkeypatch.setattr(wc.market, "drop_forming_candle", lambda cs: cs[:-1])


# --- disabled cache (path=None) ----------------------------------------------

def test_disabled_cache_always_full_fetches(monkeypatch):
    fetched = _series(_now_ms(), 11)
    calls: list = []
    _mock_market(monkeypatch, fetched, calls)
    cache = WarmupCache(None)

    out = cache.get_warmup(client=None, symbol="BTCUSDT", interval=_INTERVAL, limit=10)
    assert calls == [11]               # limit + 1
    assert out == fetched[:-1]
    cache.update("BTCUSDT", _INTERVAL, _candle(_now_ms()))   # no-op
    cache.save()                                             # no-op


# --- get_warmup --------------------------------------------------------------

def test_full_fetch_when_empty_then_populates_buffer(tmp_path, monkeypatch):
    fetched = _series(_now_ms(), 11)
    _mock_market(monkeypatch, fetched)
    cache = WarmupCache(str(tmp_path / "wc.json"))

    out = cache.get_warmup(client=None, symbol="BTCUSDT", interval=_INTERVAL, limit=10)
    assert out == fetched[:-1]
    # Buffer now in memory and survives a save/reload.
    cache.save()
    assert WarmupCache(str(tmp_path / "wc.json"))._buffers["BTCUSDT|4h"] == fetched[:-1]


def test_cache_hit_makes_no_rest_call(tmp_path, monkeypatch):
    path = str(tmp_path / "wc.json")
    cached = _series(_now_ms() - _INTERVAL_MS, 10)   # last candle one interval ago
    cache = WarmupCache(path)
    cache._buffers["BTCUSDT|4h"] = cached

    def _boom(*a, **k):
        raise AssertionError("REST call must not happen on a cache hit")

    monkeypatch.setattr("core.warmup_cache.market.get_futures_ohlcv", _boom)
    out = cache.get_warmup(client=None, symbol="BTCUSDT", interval=_INTERVAL, limit=10)
    assert out == cached


def test_gap_fill_fetches_only_missing(tmp_path, monkeypatch):
    last_open = _now_ms() - 5 * _INTERVAL_MS
    cached = _series(last_open, 10)
    fresh = _series(last_open + 6 * _INTERVAL_MS, 7)   # continues series + forming
    calls: list = []
    _mock_market(monkeypatch, fresh, calls)

    cache = WarmupCache(str(tmp_path / "wc.json"))
    cache._buffers["BTCUSDT|4h"] = cached
    out = cache.get_warmup(client=None, symbol="BTCUSDT", interval=_INTERVAL, limit=10)

    assert calls == [7]                              # elapsed(5) + 2
    assert len(out) == 10                            # trimmed to limit
    assert [c["open_time"] for c in out] == sorted(c["open_time"] for c in out)
    assert out[-1]["open_time"] == fresh[-2]["open_time"]   # newest closed present


def test_stale_beyond_limit_does_full_fetch(tmp_path, monkeypatch):
    fetched = _series(_now_ms(), 11)
    calls: list = []
    _mock_market(monkeypatch, fetched, calls)

    cache = WarmupCache(str(tmp_path / "wc.json"))
    cache._buffers["BTCUSDT|4h"] = _series(_now_ms() - 50 * _INTERVAL_MS, 10)
    out = cache.get_warmup(client=None, symbol="BTCUSDT", interval=_INTERVAL, limit=10)
    assert calls == [11]               # full fetch, not a gap fetch
    assert out == fetched[:-1]


def test_short_cache_does_full_fetch(tmp_path, monkeypatch):
    fetched = _series(_now_ms(), 11)
    calls: list = []
    _mock_market(monkeypatch, fetched, calls)

    cache = WarmupCache(str(tmp_path / "wc.json"))
    cache._buffers["BTCUSDT|4h"] = _series(_now_ms() - _INTERVAL_MS, 4)   # < limit
    out = cache.get_warmup(client=None, symbol="BTCUSDT", interval=_INTERVAL, limit=10)
    assert calls == [11]
    assert out == fetched[:-1]


# --- update ------------------------------------------------------------------

def test_update_appends_and_trims_to_limit(tmp_path, monkeypatch):
    cache = WarmupCache(str(tmp_path / "wc.json"))
    base = _now_ms() - 10 * _INTERVAL_MS
    # get_warmup records the limit (=10) used by update's trim.
    _mock_market(monkeypatch, _series(base, 11))
    cache.get_warmup(client=None, symbol="BTCUSDT", interval=_INTERVAL, limit=10)

    buf = cache._buffers["BTCUSDT|4h"]
    last = buf[-1]["open_time"]
    cache.update("BTCUSDT", _INTERVAL, _candle(last + _INTERVAL_MS))
    assert len(cache._buffers["BTCUSDT|4h"]) == 10            # still capped
    assert cache._buffers["BTCUSDT|4h"][-1]["open_time"] == last + _INTERVAL_MS


def test_update_replaces_candle_with_same_open_time(tmp_path):
    cache = WarmupCache(str(tmp_path / "wc.json"))
    cache._buffers["BTCUSDT|4h"] = _series(_now_ms(), 5)
    last_open = cache._buffers["BTCUSDT|4h"][-1]["open_time"]
    cache.update("BTCUSDT", _INTERVAL, _candle(last_open, price="999"))
    assert len(cache._buffers["BTCUSDT|4h"]) == 5
    assert cache._buffers["BTCUSDT|4h"][-1]["close"] == Decimal("999")


# --- save / load -------------------------------------------------------------

def test_save_load_roundtrip_single_file(tmp_path):
    path = str(tmp_path / "wc.json")
    cache = WarmupCache(path)
    cache._buffers["BTCUSDT|4h"] = _series(_now_ms(), 3)
    cache._buffers["ETHUSDT|30m"] = _series(_now_ms(), 3)
    cache.save()

    assert (tmp_path / "wc.json").exists()          # one file, all buffers
    reloaded = WarmupCache(path)
    assert reloaded._buffers["BTCUSDT|4h"] == cache._buffers["BTCUSDT|4h"]
    assert reloaded._buffers["ETHUSDT|30m"] == cache._buffers["ETHUSDT|30m"]
    assert all(isinstance(c["close"], Decimal)
               for c in reloaded._buffers["BTCUSDT|4h"])


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "wc.json"
    path.write_text("{not valid json")
    cache = WarmupCache(str(path))
    assert cache._buffers == {}


def test_version_mismatch_starts_empty(tmp_path):
    import json
    path = tmp_path / "wc.json"
    path.write_text(json.dumps({"version": 999, "buffers": {"X|1h": []}}))
    cache = WarmupCache(str(path))
    assert cache._buffers == {}


def test_merge_dedups_by_open_time():
    base = _now_ms()
    a = _series(base, 5)
    b = _series(base + 2 * _INTERVAL_MS, 5)         # overlaps a's last 3
    merged = _merge(a, b)
    times = [c["open_time"] for c in merged]
    assert times == sorted(set(times))
    assert len(merged) == 7                          # 5 + 5 - 3 overlap
