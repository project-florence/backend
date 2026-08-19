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
6. tools: down-tolerance markers on external failure.
7. cron: _due_digest_slot window logic.
8. cron: dedup — existing (date, slot) row short-circuits generation.
"""

from datetime import date, datetime, timezone

import pytest

from src.services.digest import agent as agent_module
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
    monkeypatch.setattr(
        "src.finance.finance_service.get_quotes", _raiser, raising=False
    )
    out = await tools_module.get_economy_quotes()
    assert out == {}


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
