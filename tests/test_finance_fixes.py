"""Regression tests for the BUGS.md backend fixes (2026-08).

Covers:
- Bug 1: ``load_records_many`` batches N symbols into a constant number of
  queries (was 3 queries per symbol).
- Bug 2: ``get_candles`` is cache-first and populates the cache on miss.
- Bug 3: ``refresh_symbols`` (partial) must not clobber the full quotes cache.
- Bug 4: ``restore_from_status`` rehydrates the circuit breaker at startup.
"""

from datetime import datetime, timedelta, timezone

import pytest

import src.finance.storage as storage
from src.finance import FinanceService
from src.finance.models import Candle, ProviderName, ProviderStatus
from src.finance.providers.genelpara import GenelParaProvider
from src.finance.providers.registry import provider
from tests.helpers import GENELPARA_URL, USD_ITEM, mock_storage

import respx
from httpx import Response


# ---------------------------------------------------------------------------
# Bug 1 — records N+1 -> batched
# ---------------------------------------------------------------------------


def _make_candle(symbol: str, ts: datetime, close: float) -> Candle:
    return Candle(
        symbol=symbol, interval="1d", ts=ts, open=close, high=close,
        low=close, close=close, volume=None, source=None,
    )


class _FakeCursor:
    """Async cursor recording every executed query and faking fetch results."""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=None):
        self._db.calls.append(query)
        if "DISTINCT ON" in query:
            self._rows = self._db.last_rows
        else:
            self._rows = self._db.agg_rows

    async def fetchall(self):
        return self._rows


class _FakeDB:
    def __init__(self, agg_rows, last_rows):
        self.agg_rows = agg_rows
        self.last_rows = last_rows
        self.calls = []

    def cursor(self, row_factory=None):
        return _FakeCursor(self)


async def test_load_records_many_batches_constant_queries(monkeypatch):
    symbol = "USD"
    now = datetime.now(timezone.utc)
    agg_rows = [
        {
            "symbol": "USD", "at_high": 42.0, "at_low": 38.0,
            "w_high": 41.0, "w_low": 39.0,
        },
        {
            "symbol": "EUR", "at_high": 46.0, "at_low": 44.0,
            "w_high": 45.5, "w_low": 44.5,
        },
    ]
    last_rows = [{"symbol": "USD", "close": 40.5}, {"symbol": "EUR", "close": 45.0}]
    fake = _FakeDB(agg_rows, last_rows)
    monkeypatch.setattr(storage, "db", fake)

    out = await storage.load_records_many(["USD", "EUR"])

    # Constant query count: every symbol must NOT multiply the query count.
    assert len(fake.calls) == 2, f"expected 2 batched queries, got {len(fake.calls)}"
    assert fake.calls[0].startswith("SELECT symbol,") and "FILTER" in fake.calls[0]
    assert "DISTINCT ON" in fake.calls[1]

    rec = out["USD"]
    assert rec["all_time_high"] == 42.0 and rec["all_time_low"] == 38.0
    assert rec["high_52w"] == 41.0 and rec["low_52w"] == 39.0
    assert rec["last_close"] == 40.5
    # rank_in_52w = (close - low52) / (high52 - low52) = (40.5-39)/(41-39) = 0.75
    assert rec["rank_in_52w"] == pytest.approx(0.75)
    # Single-symbol wrapper keeps the same shape.
    single = await storage.load_records("USD")
    assert single["symbol"] == "USD" and single["all_time_high"] == 42.0


async def test_load_records_many_drops_unknown_symbols(monkeypatch):
    fake = _FakeDB([], [])
    monkeypatch.setattr(storage, "db", fake)
    out = await storage.load_records_many(["NOT_A_SYMBOL"])
    assert out == {}
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Bug 2 — get_candles cache-first + populate on miss
# ---------------------------------------------------------------------------


async def test_get_candles_cache_hit_skips_db(monkeypatch):
    ts1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ts2 = datetime(2026, 8, 10, tzinfo=timezone.utc)
    cached = [_make_candle("USD", ts1, 40.0), _make_candle("USD", ts2, 40.5)]
    calls = {"cache": 0, "load": 0, "set": 0}

    async def _get_cached(symbol, interval):
        calls["cache"] += 1
        return cached

    async def _load(*a, **k):
        calls["load"] += 1
        return []

    async def _set(*a, **k):
        calls["set"] += 1

    monkeypatch.setattr(storage, "get_candles_cache", _get_cached)
    monkeypatch.setattr(storage, "load_candles", _load)
    monkeypatch.setattr(storage, "set_candles_cache", _set)

    out = await FinanceService().get_candles(
        "USD", "1d", start=ts2 - timedelta(days=1), end=ts2,
    )
    assert calls["load"] == 0, "DB must not be touched on a cache hit"
    assert calls["set"] == 0
    assert [c.ts for c in out] == [ts2]  # window-filtered in memory


async def test_get_candles_miss_loads_full_series_and_populates_cache(monkeypatch):
    full = [
        _make_candle("USD", datetime(2026, 8, 1, tzinfo=timezone.utc), 40.0),
        _make_candle("USD", datetime(2026, 8, 10, tzinfo=timezone.utc), 40.5),
    ]
    captured = []

    async def _get_cached(symbol, interval):
        return None

    async def _load(symbol, interval, start=None, end=None):
        # Full, window-agnostic load -> cache stays correct for any window.
        assert start is None and end is None
        return full

    async def _set(symbol, interval, candles):
        captured.append(candles)

    monkeypatch.setattr(storage, "get_candles_cache", _get_cached)
    monkeypatch.setattr(storage, "load_candles", _load)
    monkeypatch.setattr(storage, "set_candles_cache", _set)

    out = await FinanceService().get_candles("USD", "1d")
    assert len(captured) == 1 and captured[0] is full
    assert out == full


# ---------------------------------------------------------------------------
# Bug 3 — refresh_symbols partial refresh must not clobber the quotes cache
# ---------------------------------------------------------------------------


async def test_refresh_symbols_does_not_clobber_full_cache(monkeypatch):
    mock_storage(monkeypatch)
    set_cache_calls = []

    async def _set_quotes_cache(bundle, ttl):
        set_cache_calls.append(1)

    monkeypatch.setattr(storage, "set_quotes_cache", _set_quotes_cache)

    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(
            return_value=Response(200, json={"success": True, "remaining": 500, "data": {"USD": USD_ITEM}})
        )
        await FinanceService().refresh_symbols({"USD"})
        assert set_cache_calls == []  # partial set must NOT overwrite full cache

        # Opt-in write_cache=True persists to the cache (documents the hook).
        await FinanceService().refresh_symbols({"USD"}, write_cache=True)
        assert len(set_cache_calls) == 1


# ---------------------------------------------------------------------------
# Bug 4 — circuit breaker restored from persisted health on startup
# ---------------------------------------------------------------------------


def test_circuit_restore_reopens_when_cooldown_not_elapsed():
    p = GenelParaProvider()
    status = ProviderStatus(
        provider=p.name,
        last_error=datetime.now(timezone.utc) - timedelta(seconds=60),
        consecutive_failures=3,
        circuit_open=True,
    )
    p.restore_from_status(status)
    # cooldown (600 s) not elapsed -> circuit stays open, no blind full tries.
    assert p.is_available is False
    assert p._circuit.consecutive_failures == 3


def test_circuit_restore_half_open_when_cooldown_elapsed():
    p = GenelParaProvider()
    status = ProviderStatus(
        provider=p.name,
        last_error=datetime.now(timezone.utc) - timedelta(seconds=1000),
        consecutive_failures=3,
        circuit_open=True,
    )
    p.restore_from_status(status)
    # cooldown elapsed -> left half-open so a successful probe resets it.
    assert p.is_available is True


async def test_service_restore_wires_persisted_state_into_providers(monkeypatch):
    # Point restore_circuit_breakers' storage reads at a fake persisted row.
    async def _load_statuses():
        return {
            provider(ProviderName.GENELPARA).name.value: ProviderStatus(
                provider=ProviderName.GENELPARA,
                last_error=datetime.now(timezone.utc) - timedelta(seconds=10),
                consecutive_failures=5,
                circuit_open=True,
                last_error_msg="persisted outage",
            )
        }

    async def _no_redis_status(name):
        return None

    monkeypatch.setattr(storage, "load_provider_statuses", _load_statuses)
    monkeypatch.setattr(storage, "get_provider_status_cache", _no_redis_status)

    g = provider(ProviderName.GENELPARA)
    g._circuit = type(g._circuit)()  # fresh in-memory state (fresh process)
    assert g.is_available is True

    await FinanceService().restore_circuit_breakers()

    assert g.is_available is False  # restored from persisted row
    assert g._circuit.consecutive_failures == 5
