"""Shared autouse fixtures for the finance pipeline unit-test suite.

Every test in this suite is a pure unit test: providers, the service,
storage and Redis are all isolated from the network / DB / Redis. The
autouse fixtures below guarantee that:

- ``src.clients.http``'s module-level client singleton is dropped before and
  after every test, so a respx mock transport installed inside a test always
  wins (a client built outside any mock scope must never leak a real
  transport into a later test).
- provider circuit state on the shared ``PROVIDERS`` singletons is reset
  between tests (a circuit opened by one test must not poison the next).
- the yfinance-backed providers never touch the network in unit tests: their
  ``fetch_quotes`` / ``fetch_candles`` are stubbed on the shared instances
  only (instance attributes shadow the class methods; monkeypatch restores).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_helpers import FakeDB, FakeRedis  # noqa: E402
from src.clients import http as http_client_module  # noqa: E402
from src.finance.models import ProviderName  # noqa: E402
from src.finance.providers.base import CircuitState  # noqa: E402
from src.finance.providers.registry import PROVIDERS, provider  # noqa: E402


@pytest.fixture
def fake_db(monkeypatch):
    """Patch the shared async db singleton with an in-memory fake."""
    from src.core import database as db_module

    fdb = FakeDB()
    monkeypatch.setattr(db_module.db, "cursor", fdb.cursor)
    monkeypatch.setattr(db_module.db, "commit", fdb.commit)
    monkeypatch.setattr(db_module.db, "rollback", fdb.rollback)
    monkeypatch.setattr(db_module.db, "release_current", fdb.release_current)
    return fdb


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch the shared Redis proxy singleton with an in-memory fake."""
    from src.core import redis as redis_module

    fr = FakeRedis()
    for name in (
        "get",
        "set",
        "delete",
        "incr",
        "expire",
        "sadd",
        "srem",
        "smembers",
        "sismember",
    ):
        monkeypatch.setattr(redis_module.r, name, getattr(fr, name))
    return fr


@pytest.fixture(autouse=True)
def _isolate_http_client():
    """Recreate the shared httpx client per test so respx always wins."""
    http_client_module._client = None
    yield
    http_client_module._client = None


@pytest.fixture(autouse=True)
def _reset_provider_circuits():
    """Fresh circuit state on the shared provider singletons per test.

    Also clears GenelPara's ``last_remaining`` quota memory so a successful
    envelope in one test cannot leak its quota into the next test's bundle.
    """
    for p in PROVIDERS.values():
        p._circuit = CircuitState()
        if hasattr(p, "last_remaining"):
            setattr(p, "last_remaining", None)
    yield
    for p in PROVIDERS.values():
        p._circuit = CircuitState()
        if hasattr(p, "last_remaining"):
            setattr(p, "last_remaining", None)


@pytest.fixture(autouse=True)
def _stub_yfinance_network(monkeypatch):
    """yfinance is a sync network library — never called in unit tests."""

    async def _no_quotes(symbols: set[str]) -> dict:
        return {}

    async def _no_candles(symbol, interval="1d", start=None, end=None):
        return []

    for name in (ProviderName.YFINANCE_FX, ProviderName.YFINANCE_METALS):
        p = provider(name)
        if p is not None:
            monkeypatch.setattr(p, "fetch_quotes", _no_quotes)
            monkeypatch.setattr(p, "fetch_candles", _no_candles)
    yield