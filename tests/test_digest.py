"""Unit tests for the market-digest feature.

All tests are hermetic: no real DB, Redis, HTTP or LLM calls. The agent's
build path is inspected without ever triggering a model call, the generation
service is exercised against a fake agent + fake db + fake redis, tools are
checked for down-tolerance, and cron window/dedup logic is frozen in time via
monkeypatched ``datetime``.

Covers:
1. models: Digest/DigestSection construct + dump round-trip + defaults.
2. agent: _build_agent wiring (output type + model_settings, reasoning off).
3. service: slot validation raises ValueError on an invalid slot.
4. service: happy path normalizes + writes Redis + inserts into digests.
5. service: down-tolerance — Redis/DB failures never propagate.
6. service: hard timeout — a slow agent.run raises TimeoutError, never hangs.
7. tools: down-tolerance markers on external failure.
8. tools: economy_quotes + gainers/losers are cache-only (no refresh trigger).
9. cron: _due_digest_slot window logic.
10. cron: dedup — existing (date, slot) row short-circuits generation.
"""

import json
from datetime import date, datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from src.api import deps as deps_module
from src.api.digest import router as digest_router
from src.services.digest import agent as agent_module
from src.services.digest import reads as reads_module
from src.services.digest import service as service_module
from src.services.digest import tools as tools_module
from src.services.digest.agent import _build_agent
from src.services.digest.models import Digest, DigestSection
from src.services.digest.service import generate_digest
from src.cron import tasks as cron_tasks
from src.cron.tasks import DIGEST_TZ, _due_digest_slot


# ---------------------------------------------------------------------------
# Fake db / cursor / agent helpers
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, db):
        self._db = db
        self.executed = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=None):
        self.executed = query
        self._db.queries.append((query, params))

    async def fetchone(self):
        return self._db.fetchone_result

    async def fetchall(self):
        return self._db.fetchall_result


class _FakeDB:
    def __init__(self):
        self.queries = []
        self.commit_calls = 0
        self.fetchone_result = None
        self.fetchall_result = []

    def cursor(self, row_factory=None):
        return _FakeCursor(self)

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        pass

    async def release_current(self):
        pass


class _FakeResult:
    def __init__(self, output):
        self.output = output


class _FakeAgent:
    def __init__(self, output):
        self._output = output

    async def run(self, *args, **kwargs):
        return _FakeResult(self._output)


def _make_digest(**overrides) -> Digest:
    base = dict(
        date=date(2026, 8, 19),
        slot="morning",
        title="Test Bülteni",
        content="İçerik",
        sections=[DigestSection(heading="Piyasa", body="Özet")],
        metadata={"source": "test"},
        language="tr",
    )
    base.update(overrides)
    return Digest(**base)


def _patch_agent(monkeypatch, output: Digest) -> None:
    monkeypatch.setattr(service_module, "_build_agent", lambda: _FakeAgent(output))


def _patch_precollect(monkeypatch) -> None:
    async def _no_snapshot() -> dict:
        return {}

    async def _no_news() -> list:
        return []

    monkeypatch.setattr(service_module.tools, "get_market_snapshot", _no_snapshot)
    monkeypatch.setattr(service_module.tools, "get_news_feed", _no_news)


# ---------------------------------------------------------------------------
# 1. models
# ---------------------------------------------------------------------------


def test_digest_section_construct_and_dump():
    s = DigestSection(heading="Makro", body="Detay")
    assert s.heading == "Makro"
    assert s.body == "Detay"
    assert s.model_dump() == {"heading": "Makro", "body": "Detay"}


def test_digest_defaults():
    d = Digest(date=date(2026, 8, 19), slot="noon", title="T", content="C")
    assert d.language == "tr"
    assert d.sections == []
    assert d.metadata == {}
    assert d.id != ""
    assert d.created_at is not None


def test_digest_dump_round_trip():
    d = _make_digest()
    payload = d.model_dump()
    assert payload["date"] == date(2026, 8, 19)
    assert payload["slot"] == "morning"
    assert payload["language"] == "tr"
    assert payload["sections"][0]["heading"] == "Piyasa"
    rebuilt = Digest(**payload)
    assert rebuilt.id == d.id
    assert rebuilt.sections[0].body == "Özet"
    assert rebuilt.created_at == d.created_at


# ---------------------------------------------------------------------------
# 2. agent wiring (no LLM call)
# ---------------------------------------------------------------------------


def test_build_agent_output_type_and_model_settings():
    agent = _build_agent()
    assert agent is not None
    assert agent.output_type is Digest
    assert agent.model_settings == {
        "openai_reasoning_effort": "none",
        "parallel_tool_calls": False,
    }
    tool_names = set(agent._function_toolset.tools)
    assert tool_names == {
        "search_news",
        "fetch_article_text",
    }


# ---------------------------------------------------------------------------
# 3. service slot validation
# ---------------------------------------------------------------------------


async def test_generate_digest_rejects_invalid_slot():
    with pytest.raises(ValueError, match="Unknown digest slot"):
        await generate_digest(slot="midnight")


# ---------------------------------------------------------------------------
# 4. service happy path
# ---------------------------------------------------------------------------


async def test_generate_digest_happy_path(monkeypatch):
    prebuilt = _make_digest()
    _patch_agent(monkeypatch, prebuilt)
    _patch_precollect(monkeypatch)

    fake_db = _FakeDB()
    monkeypatch.setattr(service_module, "db", fake_db)

    redis_calls = []
    async def _fake_set(key, value, ex=None):
        redis_calls.append((key, value, ex))
    monkeypatch.setattr("src.core.redis.r.set", _fake_set)

    digest = await generate_digest(slot="morning")

    assert digest.id != ""
    assert digest.slot == "morning"
    assert digest.created_at is not None
    assert digest.language == "tr"
    assert digest.date == date.today()
    assert digest.metadata["slot"] == "morning"
    assert "generated_at" in digest.metadata

    assert len(redis_calls) == 1
    key, value, ex = redis_calls[0]
    assert key == "current_digest"
    assert ex == 14400
    assert '"slot": "morning"' in value

    insert = [q for q in fake_db.queries if "INSERT INTO digests" in q[0]]
    assert len(insert) == 1
    assert insert[0][1][0] == digest.id
    assert fake_db.commit_calls == 1


# ---------------------------------------------------------------------------
# 5. service down-tolerance
# ---------------------------------------------------------------------------


async def test_generate_digest_tolerates_redis_and_db_failure(monkeypatch):
    _patch_agent(monkeypatch, _make_digest())
    _patch_precollect(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("redis down")
    monkeypatch.setattr("src.core.redis.r.set", _boom)

    class _BoomDB:
        def cursor(self, row_factory=None):
            raise RuntimeError("db down")
    monkeypatch.setattr(service_module, "db", _BoomDB())

    digest = await generate_digest(slot="morning")
    assert digest is not None
    assert digest.slot == "morning"
    assert digest.language == "tr"


async def test_generate_digest_times_out_instead_of_hanging(monkeypatch):
    import asyncio

    _patch_precollect(monkeypatch)

    def _config():
        return {
            "digest": {
                "slot_times": {"morning": "09:45", "noon": "13:15", "evening": "18:45"},
                "redis_key": "current_digest",
                "redis_ttl": 14400,
                "max_requests": 200,
                "timeout_s": 0.05,
            }
        }

    monkeypatch.setattr(service_module, "get_config", _config)

    class _SlowAgent:
        async def run(self, *args, **kwargs):
            await asyncio.sleep(10)
            raise AssertionError("agent must be cancelled by the timeout")

    monkeypatch.setattr(service_module, "_build_agent", lambda: _SlowAgent())

    with pytest.raises(asyncio.TimeoutError):
        await generate_digest(slot="morning")


async def test_generate_digest_config_has_timeout_default():
    from src.core.config import get_config

    assert get_config()["digest"]["timeout_s"] == 3600
    assert get_config()["digest"]["max_tool_calls"] == 6
    assert "max_tokens_total" not in get_config()["digest"]


async def test_generate_digest_usage_limits_from_config(monkeypatch):
    from pydantic_ai.usage import UsageLimits

    _patch_precollect(monkeypatch)

    captured = {}

    def _config():
        return {
            "digest": {
                "slot_times": {"morning": "09:45", "noon": "13:15", "evening": "18:45"},
                "redis_key": "current_digest",
                "redis_ttl": 14400,
                "max_requests": 7,
                "max_tool_calls": 6,
                "timeout_s": 0.05,
            }
        }

    monkeypatch.setattr(service_module, "get_config", _config)

    class _CaptureAgent:
        async def run(self, *args, **kwargs):
            captured["limits"] = kwargs["usage_limits"]
            return _FakeResult(_make_digest())

    monkeypatch.setattr(service_module, "_build_agent", lambda: _CaptureAgent())

    await generate_digest(slot="morning")

    assert isinstance(captured["limits"], UsageLimits)
    assert captured["limits"].request_limit == 7
    assert captured["limits"].tool_calls_limit == 6
    assert captured["limits"].total_tokens_limit is None


# ---------------------------------------------------------------------------
# 6. tools down-tolerance
# ---------------------------------------------------------------------------


async def _raiser(*args, **kwargs):
    raise RuntimeError("external down")


async def test_tool_get_market_status_down_tolerant(monkeypatch):
    monkeypatch.setattr(
        "src.services.market.get_market_status_payload", _raiser, raising=False
    )
    out = await tools_module.get_market_status()
    assert out == {"unavailable": True}


async def test_tool_get_news_feed_down_tolerant(monkeypatch):
    monkeypatch.setattr(
        "src.services.marketfeed.get_news_feed", _raiser, raising=False
    )
    out = await tools_module.get_news_feed()
    assert out == []


async def test_tool_get_economy_quotes_down_tolerant(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("external down")

    monkeypatch.setattr("src.finance.storage.get_quotes_cache", _boom)
    monkeypatch.setattr("src.finance.storage.load_latest_db_snapshot", _boom)
    out = await tools_module.get_economy_quotes()
    assert out == {}


def _quote_bundle(quotes: dict) -> "QuoteBundle":
    from datetime import datetime, timezone

    from src.finance.models import ProviderName, Quote, QuoteBundle

    ts = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    return QuoteBundle(
        ts=ts,
        source=ProviderName.GENELPARA,
        quotes={
            sym: Quote(symbol=sym, buying=val, price=val, change_pct=pct, source=ProviderName.GENELPARA, ts=ts)
            for sym, (val, pct) in quotes.items()
        },
    )


async def test_tool_get_economy_quotes_cache_only(monkeypatch):
    bundle = _quote_bundle({"USD": (41.0, 0.5), "EUR": (44.0, 1.0)})

    async def _cached() -> "QuoteBundle":
        return bundle

    async def _no_snapshot(symbols):
        return {}

    called = {"refresh": False}
    async def _must_not_refresh(*args, **kwargs):
        called["refresh"] = True
        raise AssertionError("refresh must not be triggered")

    monkeypatch.setattr("src.finance.storage.get_quotes_cache", _cached)
    monkeypatch.setattr("src.finance.storage.load_latest_db_snapshot", _no_snapshot)
    monkeypatch.setattr("src.finance.finance_service.get_quotes", _must_not_refresh, raising=False)

    out = await tools_module.get_economy_quotes()

    assert out["USD"] == {"last": 41.0, "change_pct": 0.5}
    assert out["EUR"] == {"last": 44.0, "change_pct": 1.0}
    assert called["refresh"] is False


async def test_tool_get_economy_quotes_db_snapshot_fallback(monkeypatch):
    from datetime import datetime, timezone

    from src.finance.models import ProviderName, Quote

    bundle = _quote_bundle({"USD": (41.0, 0.5)})
    ts = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    snapshot = {
        "GBP": Quote(symbol="GBP", buying=48.0, price=48.0, change_pct=-0.2, source=ProviderName.DB_SNAPSHOT, ts=ts, stale=True),
    }

    async def _cached() -> "QuoteBundle":
        return bundle

    async def _snapshot(symbols):
        return {sym: q for sym, q in snapshot.items() if sym in symbols}

    called = {"refresh": False}
    async def _must_not_refresh(*args, **kwargs):
        called["refresh"] = True
        raise AssertionError("refresh must not be triggered")

    monkeypatch.setattr("src.finance.storage.get_quotes_cache", _cached)
    monkeypatch.setattr("src.finance.storage.load_latest_db_snapshot", _snapshot)
    monkeypatch.setattr("src.finance.finance_service.get_quotes", _must_not_refresh, raising=False)

    out = await tools_module.get_economy_quotes()

    assert out["USD"] == {"last": 41.0, "change_pct": 0.5}
    assert out["GBP"] == {"last": 48.0, "change_pct": -0.2}
    assert called["refresh"] is False


async def test_tool_get_gainers_losers_down_tolerant(monkeypatch):
    class _BoomRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

    monkeypatch.setattr("src.core.redis.r", _BoomRedis())
    out = await tools_module.get_gainers_losers()
    assert out == {"gainers": [], "losers": []}


async def test_tool_get_gainers_losers_empty_when_no_cache(monkeypatch):
    class _EmptyRedis:
        async def get(self, key):
            return None

    called = {"summary": False}
    async def _must_not_fetch(*args, **kwargs):
        called["summary"] = True
        raise AssertionError("company fetch must not be triggered")

    monkeypatch.setattr("src.core.redis.r", _EmptyRedis())
    monkeypatch.setattr("src.services.company.get_companies_summary", _must_not_fetch, raising=False)

    out = await tools_module.get_gainers_losers()

    assert out == {"gainers": [], "losers": []}
    assert called["summary"] is False


async def test_tool_get_gainers_losers_from_cached_profiles(monkeypatch):
    companies = json.dumps(
        [{"ticker": t, "name": t} for t in ("A", "B", "C", "D", "E", "F")]
    )
    profiles = {
        "A.IS": json.dumps({"market": {"currentPrice": 110.0, "previousClose": 100.0}}),   # +10.0
        "B.IS": json.dumps({"market": {"currentPrice": 90.0, "previousClose": 100.0}}),    # -10.0
        "C.IS": json.dumps({"market": {"currentPrice": 105.0, "previousClose": 100.0}}),   # +5.0
        "D.IS": json.dumps({"market": {"currentPrice": 97.0, "previousClose": 100.0}}),    # -3.0
        "E.IS": json.dumps({"market": {"currentPrice": 101.0, "previousClose": 100.0}}),   # +1.0
        "F.IS": json.dumps({"market": {"currentPrice": 92.0, "previousClose": 100.0}}),    # -8.0
    }

    class _CachedRedis:
        async def get(self, key):
            if key == "companies":
                return companies
            return profiles.get(key)

    monkeypatch.setattr("src.core.redis.r", _CachedRedis())

    out = await tools_module.get_gainers_losers()

    assert [g["ticker"] for g in out["gainers"]] == ["A", "C", "E", "D", "F"]
    assert [l["ticker"] for l in out["losers"]] == ["B", "F", "D", "E", "C"]
    assert out["gainers"][0] == {"ticker": "A", "last_price": 110.0, "change_pct": pytest.approx(10.0)}
    assert out["losers"][0] == {"ticker": "B", "last_price": 90.0, "change_pct": pytest.approx(-10.0)}


async def test_tool_get_gainers_losers_skips_profiles_without_prices(monkeypatch):
    companies = json.dumps([{"ticker": "A"}, {"ticker": "B"}])
    profiles = {
        "A.IS": json.dumps({"market": {"currentPrice": 110.0, "previousClose": 100.0}}),
        "B.IS": json.dumps({"market": {"currentPrice": None, "previousClose": 100.0}}),
    }

    class _CachedRedis:
        async def get(self, key):
            if key == "companies":
                return companies
            return profiles.get(key)

    monkeypatch.setattr("src.core.redis.r", _CachedRedis())

    out = await tools_module.get_gainers_losers()

    assert [g["ticker"] for g in out["gainers"]] == ["A"]
    assert [l["ticker"] for l in out["losers"]] == ["A"]


async def test_tool_search_news_down_tolerant(monkeypatch):
    monkeypatch.setattr("src.clients.search.news_search", _raiser, raising=False)
    out = await tools_module.search_news("test")
    assert out == []


# ---------------------------------------------------------------------------
# 7. cron window logic
# ---------------------------------------------------------------------------


class _FakeDatetime(datetime):
    _fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def _freeze_time(monkeypatch, hhmm: str) -> None:
    hour, minute = map(int, hhmm.split(":"))
    _FakeDatetime._fixed = datetime(
        2026, 8, 19, hour, minute, tzinfo=DIGEST_TZ
    )
    monkeypatch.setattr(cron_tasks, "datetime", _FakeDatetime)


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [
        ("09:35", "morning"),   # morning window 09:30-09:45
        ("13:05", "noon"),      # noon window 13:00-13:15
        ("18:40", "evening"),   # evening window 18:30-18:45
        ("20:00", None),        # outside any window
        ("09:20", None),        # before the morning lead
        ("09:45", None),        # exactly at end -> not < end
    ],
)
def test_due_digest_slot_windows(monkeypatch, hhmm, expected):
    _freeze_time(monkeypatch, hhmm)
    assert _due_digest_slot() == expected


# ---------------------------------------------------------------------------
# 8. cron dedup
# ---------------------------------------------------------------------------


async def test_run_market_digest_skips_when_row_exists(monkeypatch):
    _freeze_time(monkeypatch, "09:35")
    monkeypatch.setattr(cron_tasks, "_due_digest_slot", lambda: "morning")

    fake_db = _FakeDB()
    fake_db.fetchone_result = (1,)
    monkeypatch.setattr(cron_tasks, "db", fake_db)

    called = {"generate": False}
    async def _should_not_be_called(*args, **kwargs):
        called["generate"] = True
        raise AssertionError("generate_digest must not run on dedup")
    monkeypatch.setattr(service_module, "generate_digest", _should_not_be_called)

    await cron_tasks.run_market_digest()

    assert called["generate"] is False
    select = [q for q in fake_db.queries if "SELECT 1 FROM digests" in q[0]]
    assert len(select) == 1
    assert select[0][1] == (date(2026, 8, 19), "morning")


# ---------------------------------------------------------------------------
# 9. read helpers (src/services/digest/reads.py)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, value=None, error=False):
        self.value = value
        self.error = error
        self.called_with = None

    async def get(self, key):
        self.called_with = key
        if self.error:
            raise RuntimeError("redis down")
        return self.value


def _row(**overrides) -> dict:
    row = dict(
        id="abc123",
        date=date(2026, 8, 19),
        slot="morning",
        title="Test Bülteni",
        content="İçerik",
        sections=[{"heading": "Piyasa", "body": "Özet"}],
        metadata={"source": "test"},
        language="tr",
        created_at=datetime(2026, 8, 19, 9, 45, tzinfo=timezone.utc),
    )
    row.update(overrides)
    return row


async def test_get_current_digest_happy_path(monkeypatch):
    digest = _make_digest()
    payload = json.dumps(digest.model_dump(mode="json"))
    fake_redis = _FakeRedis(value=payload)
    monkeypatch.setattr(reads_module, "r", fake_redis)

    out = await reads_module.get_current_digest()

    assert fake_redis.called_with == "current_digest"
    assert isinstance(out, Digest)
    assert out.id == digest.id
    assert out.date == digest.date
    assert out.slot == digest.slot
    assert out.title == digest.title
    assert out.sections[0].heading == "Piyasa"
    assert out.metadata == {"source": "test"}


async def test_get_current_digest_missing_invalid_or_down(monkeypatch):
    monkeypatch.setattr(reads_module, "r", _FakeRedis(value=None))
    assert await reads_module.get_current_digest() is None

    monkeypatch.setattr(reads_module, "r", _FakeRedis(value="not-json{"))
    assert await reads_module.get_current_digest() is None

    monkeypatch.setattr(reads_module, "r", _FakeRedis(value="{}"))
    assert await reads_module.get_current_digest() is None

    monkeypatch.setattr(reads_module, "r", _FakeRedis(value='{"id":"x"}', error=True))
    assert await reads_module.get_current_digest() is None


async def test_get_digest_by_date_slot_happy_path(monkeypatch):
    fake_db = _FakeDB()
    fake_db.fetchone_result = _row()
    monkeypatch.setattr(reads_module, "db", fake_db)

    out = await reads_module.get_digest_by_date_slot(date(2026, 8, 19), "morning")

    assert isinstance(out, Digest)
    assert out.id == "abc123"
    assert out.date == date(2026, 8, 19)
    assert out.slot == "morning"
    assert out.sections[0].body == "Özet"
    assert fake_db.queries[0][1] == (date(2026, 8, 19), "morning")


async def test_get_digest_by_date_slot_missing_or_db_down(monkeypatch):
    fake_db = _FakeDB()
    fake_db.fetchone_result = None
    monkeypatch.setattr(reads_module, "db", fake_db)
    assert await reads_module.get_digest_by_date_slot(date(2026, 8, 19), "noon") is None

    class _BoomDB:
        def cursor(self, row_factory=None):
            raise RuntimeError("db down")
    monkeypatch.setattr(reads_module, "db", _BoomDB())
    assert await reads_module.get_digest_by_date_slot(date(2026, 8, 19), "noon") is None


async def test_get_digests_by_date_ordered_by_slot(monkeypatch):
    fake_db = _FakeDB()
    fake_db.fetchall_result = [
        _row(id="e1", slot="evening"),
        _row(id="m1", slot="morning"),
        _row(id="n1", slot="noon"),
    ]
    monkeypatch.setattr(reads_module, "db", fake_db)

    out = await reads_module.get_digests_by_date(date(2026, 8, 19))

    assert [d.slot for d in out] == ["morning", "noon", "evening"]
    assert [d.id for d in out] == ["m1", "n1", "e1"]


async def test_get_digests_by_date_empty_and_db_down(monkeypatch):
    fake_db = _FakeDB()
    fake_db.fetchall_result = []
    monkeypatch.setattr(reads_module, "db", fake_db)
    assert await reads_module.get_digests_by_date(date(2026, 8, 19)) == []

    class _BoomDB:
        def cursor(self, row_factory=None):
            raise RuntimeError("db down")
    monkeypatch.setattr(reads_module, "db", _BoomDB())
    assert await reads_module.get_digests_by_date(date(2026, 8, 19)) == []


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [
        ("05:00", "morning"),   # before first slot -> morning
        ("09:45", "morning"),   # exactly at morning slot time
        ("12:00", "morning"),   # between morning and noon
        ("13:15", "noon"),      # exactly at noon slot time
        ("16:00", "noon"),      # between noon and evening
        ("18:45", "evening"),   # exactly at evening slot time
        ("23:00", "evening"),   # after the last slot
    ],
)
def test_slot_for_datetime_windows(hhmm, expected):
    hour, minute = map(int, hhmm.split(":"))
    at = datetime(2026, 8, 19, hour, minute, tzinfo=DIGEST_TZ)
    assert reads_module._slot_for_datetime(at) == expected


async def test_get_digest_at_resolves_date_and_slot(monkeypatch):
    calls = {}

    async def _fake_by_date_slot(digest_date, slot):
        calls["date"] = digest_date
        calls["slot"] = slot
        return _make_digest(date=digest_date, slot=slot)

    monkeypatch.setattr(reads_module, "get_digest_by_date_slot", _fake_by_date_slot)

    at = datetime(2026, 8, 19, 20, 0, tzinfo=DIGEST_TZ)
    out = await reads_module.get_digest_at(at)

    assert calls == {"date": date(2026, 8, 19), "slot": "evening"}
    assert out.slot == "evening"

    # UTC-aware input still resolves to Istanbul date/slot (17:00 UTC = 20:00 IST).
    at_utc = datetime(2026, 8, 19, 17, 0, tzinfo=timezone.utc)
    await reads_module.get_digest_at(at_utc)
    assert calls == {"date": date(2026, 8, 19), "slot": "evening"}

    # Naive input is treated as Istanbul time.
    at_naive = datetime(2026, 8, 19, 12, 0)
    await reads_module.get_digest_at(at_naive)
    assert calls == {"date": date(2026, 8, 19), "slot": "morning"}


# ---------------------------------------------------------------------------
# 10. read API (src/api/digest.py)
# ---------------------------------------------------------------------------


def _build_digest_app():
    app = FastAPI()
    app.include_router(digest_router)
    app.dependency_overrides[deps_module.get_current_user] = lambda: 1
    return app


async def _get(app, url):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url)


async def test_digest_endpoint_current(monkeypatch):
    app = _build_digest_app()
    digest = _make_digest()

    async def _current():
        return digest
    monkeypatch.setattr(reads_module, "get_current_digest", _current)

    resp = await _get(app, "/digest")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == digest.id
    assert body["date"] == "2026-08-19"
    assert body["slot"] == "morning"
    assert body["title"] == digest.title
    assert body["content"] == digest.content
    assert body["sections"] == [{"heading": "Piyasa", "body": "Özet"}]
    assert body["metadata"] == {"source": "test"}
    assert body["language"] == "tr"
    assert "created_at" in body


async def test_digest_endpoint_current_404(monkeypatch):
    app = _build_digest_app()

    async def _current():
        return None
    monkeypatch.setattr(reads_module, "get_current_digest", _current)

    resp = await _get(app, "/digest")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "No digest available"


async def test_digest_endpoint_date_slot(monkeypatch):
    app = _build_digest_app()
    digest = _make_digest(slot="noon")

    async def _by_date_slot(digest_date, slot):
        assert digest_date == date(2026, 8, 19)
        assert slot == "noon"
        return digest
    monkeypatch.setattr(reads_module, "get_digest_by_date_slot", _by_date_slot)

    resp = await _get(app, "/digest?date=2026-08-19&slot=noon")

    assert resp.status_code == 200
    assert resp.json()["slot"] == "noon"


async def test_digest_endpoint_date_slot_404(monkeypatch):
    app = _build_digest_app()

    async def _missing(digest_date, slot):
        return None
    monkeypatch.setattr(reads_module, "get_digest_by_date_slot", _missing)

    resp = await _get(app, "/digest?date=2026-08-19&slot=morning")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Digest not found"


async def test_digest_endpoint_date_only_returns_array(monkeypatch):
    app = _build_digest_app()
    digests = [_make_digest(slot=s) for s in ("morning", "evening")]

    async def _by_date(digest_date):
        assert digest_date == date(2026, 8, 19)
        return digests
    monkeypatch.setattr(reads_module, "get_digests_by_date", _by_date)

    resp = await _get(app, "/digest?date=2026-08-19")

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert [d["slot"] for d in resp.json()] == ["morning", "evening"]


async def test_digest_endpoint_date_only_empty(monkeypatch):
    app = _build_digest_app()

    async def _empty(digest_date):
        return []
    monkeypatch.setattr(reads_module, "get_digests_by_date", _empty)

    resp = await _get(app, "/digest?date=2026-08-19")

    assert resp.status_code == 200
    assert resp.json() == []


async def test_digest_endpoint_at(monkeypatch):
    app = _build_digest_app()
    digest = _make_digest(slot="evening")

    async def _at(at):
        assert at.tzinfo is not None
        return digest
    monkeypatch.setattr(reads_module, "get_digest_at", _at)

    resp = await _get(app, "/digest?at=2026-08-19T20%3A00%3A00%2B03%3A00")

    assert resp.status_code == 200
    assert resp.json()["slot"] == "evening"


async def test_digest_endpoint_at_404(monkeypatch):
    app = _build_digest_app()

    async def _missing(at):
        return None
    monkeypatch.setattr(reads_module, "get_digest_at", _missing)

    resp = await _get(app, "/digest?at=2026-08-19T20%3A00%3A00%2B03%3A00")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Digest not found"


async def test_digest_endpoint_most_specific_wins(monkeypatch):
    app = _build_digest_app()
    digest = _make_digest(slot="noon")
    called = set()

    async def _by_date_slot(digest_date, slot):
        called.add("date_slot")
        return digest

    async def _by_date(digest_date):
        called.add("date")
        return []

    async def _at(at):
        called.add("at")
        return digest

    async def _current():
        called.add("current")
        return digest

    monkeypatch.setattr(reads_module, "get_digest_by_date_slot", _by_date_slot)
    monkeypatch.setattr(reads_module, "get_digests_by_date", _by_date)
    monkeypatch.setattr(reads_module, "get_digest_at", _at)
    monkeypatch.setattr(reads_module, "get_current_digest", _current)

    resp = await _get(
        app, "/digest?date=2026-08-19&slot=noon&at=2026-08-19T20%3A00%3A00%2B03%3A00"
    )

    assert resp.status_code == 200
    assert called == {"date_slot"}

    resp = await _get(app, "/digest?date=2026-08-19&at=2026-08-19T20%3A00%3A00%2B03%3A00")
    assert resp.status_code == 200
    assert called == {"date_slot", "date"}


async def test_digest_endpoint_slot_without_date_422():
    app = _build_digest_app()
    resp = await _get(app, "/digest?slot=morning")
    assert resp.status_code == 422


async def test_digest_endpoint_invalid_params_422():
    app = _build_digest_app()
    assert (await _get(app, "/digest?date=not-a-date")).status_code == 422
    assert (await _get(app, "/digest?date=2026-08-19&slot=midnight")).status_code == 422
    assert (await _get(app, "/digest?at=not-a-date")).status_code == 422
