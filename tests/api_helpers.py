"""Test-only helpers for API router unit tests.

These helpers provide in-memory stand-ins for the async ``db`` singleton
(``src.core.database.db``) and the async Redis proxy (``src.core.redis.r``),
plus a small app/HTTP client builder. They keep every router test hermetic:
no real Postgres, no real Redis, no network.
"""

from types import SimpleNamespace

import httpx
from argon2.exceptions import VerificationError
from fastapi import FastAPI

from src.api import deps as deps_module


class FakeCursor:
    def __init__(self, db):
        self._db = db
        self.query = None
        self.params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=None):
        self._db.queries.append((query, params))
        self.query = query
        self.params = params

    async def fetchone(self):
        queue = self._db._fetchone_queue
        if queue:
            return queue.pop(0)
        return self._db.fetchone_result

    async def fetchall(self):
        queue = self._db._fetchall_queue
        if queue:
            return queue.pop(0)
        return self._db.fetchall_result

    @property
    def rowcount(self):
        return self._db.rowcount


class FakeDB:
    """In-memory stand-in for the ``db`` singleton's async surface."""

    def __init__(self):
        self.queries = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.release_calls = 0
        self.fetchone_result = None
        self.fetchall_result = []
        self.rowcount = 1
        self._fetchone_queue = []
        self._fetchall_queue = []

    def cursor(self, row_factory=None, **kwargs):
        return FakeCursor(self)

    def queue_fetchone(self, *results):
        self._fetchone_queue.extend(results)

    def queue_fetchall(self, *results):
        self._fetchall_queue.extend(results)

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    async def release_current(self):
        self.release_calls += 1


class FakeRedis:
    """In-memory stand-in for the Redis proxy (dict backed)."""

    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False, xx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        return True

    async def sadd(self, key, *values):
        self.store.setdefault(key, set()).update(values)
        return len(self.store[key])

    async def srem(self, key, *values):
        members = self.store.setdefault(key, set())
        before = len(members)
        members.difference_update(values)
        return before - len(members)

    async def smembers(self, key):
        return set(self.store.get(key, []))

    async def sismember(self, key, value):
        return value in self.store.get(key, set())


class FakePasswordHasher:
    """Synchronous argon2 stand-in: ``hash:x`` maps to/from ``x``."""

    def hash(self, password: str) -> str:
        return f"hash:{password}"

    def verify(self, hashed: str, password: str):
        if hashed == f"hash:{password}":
            return True
        raise VerificationError("password mismatch")


def make_report(**overrides) -> SimpleNamespace:
    base = {
        "title": "Test Raporu",
        "about": "THYAO",
        "date": "2026-08-20T12:00:00+00:00",
        "report": "# Analiz\n\nİçerik.",
        "sentiments": [{"sentiment": "positive", "url": "http://x", "reasoning": "y"}],
        "token_usage": {"prompt": 100, "completion": 900, "total": 1000},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def build_app(router, *, user_id=7, user_full=(7, "user")):
    """Minimal FastAPI app exposing a single router with auth deps overridden."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[deps_module.get_current_user] = lambda: user_id
    app.dependency_overrides[deps_module.get_current_user_full] = lambda: user_full
    return app


async def request(app, method, url, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kwargs)