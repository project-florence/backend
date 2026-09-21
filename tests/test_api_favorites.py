"""Unit tests for src/api/favorites.py (B-16).

Hermetic: the shared db/redis singletons are faked; BIST ticker validation is
stubbed so ``validate_symbol`` never reaches a real ticker cache. Covers BIST
favorites (existing behaviour) plus canonical/legacy economy symbols and the
invalid-symbol rejection.
"""

from src.api import favorites as favorites_module
from src.api.favorites import router as favorites_router
from src.services import bist as bist_module

from api_helpers import build_app, request


def _patch_bist(monkeypatch, valid: set[str]):
    async def _is_valid(ticker):
        return ticker.upper() in valid

    monkeypatch.setattr(bist_module, "is_valid_bist_ticker", _is_valid)


def _insert_params(fake_db):
    inserts = [q for q in fake_db.queries if "INSERT INTO favorites" in q[0]]
    return inserts[-1][1]


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


async def test_add_bist_favorite(monkeypatch, fake_db, fake_redis):
    _patch_bist(monkeypatch, {"THYAO"})
    app = build_app(favorites_router)

    resp = await request(app, "POST", "/favorites/THYAO")

    assert resp.status_code == 200
    assert _insert_params(fake_db) == (7, "THYAO")


async def test_add_economy_favorite_usd(monkeypatch, fake_db, fake_redis):
    # USD kanonik registry sembolu; BIST kontrolune hic gidilmez.
    _patch_bist(monkeypatch, set())
    app = build_app(favorites_router)

    resp = await request(app, "POST", "/favorites/USD")

    assert resp.status_code == 200
    assert _insert_params(fake_db) == (7, "USD")


async def test_add_legacy_economy_favorite_normalized(monkeypatch, fake_db, fake_redis):
    # Legacy ad (frontend'in kullandigi) kanonik sembole normalize edilir.
    _patch_bist(monkeypatch, set())
    app = build_app(favorites_router)

    resp = await request(app, "POST", "/favorites/gram-altin")

    assert resp.status_code == 200
    assert _insert_params(fake_db) == (7, "XAU-GRAM")


async def test_add_invalid_symbol_rejected(monkeypatch, fake_db, fake_redis):
    _patch_bist(monkeypatch, set())
    app = build_app(favorites_router)

    resp = await request(app, "POST", "/favorites/NOPE")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "error_invalid_ticker"
    assert fake_db.queries == []


async def test_add_favorite_db_failure(monkeypatch, fake_db, fake_redis):
    _patch_bist(monkeypatch, {"THYAO"})

    class _RaisingCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(favorites_module.db, "cursor", lambda *a, **k: _RaisingCursor())

    app = build_app(favorites_router)
    resp = await request(app, "POST", "/favorites/THYAO")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "error_favorite_failed"
    assert fake_db.rollback_calls == 1


# ---------------------------------------------------------------------------
# remove / list
# ---------------------------------------------------------------------------


async def test_remove_economy_favorite(monkeypatch, fake_db, fake_redis):
    _patch_bist(monkeypatch, set())
    app = build_app(favorites_router)

    resp = await request(app, "DELETE", "/favorites/USD")

    assert resp.status_code == 200
    deletes = [q for q in fake_db.queries if "DELETE FROM favorites" in q[0]]
    assert deletes[-1][1] == (7, "USD")


async def test_remove_invalid_symbol_rejected(monkeypatch, fake_db, fake_redis):
    _patch_bist(monkeypatch, set())
    app = build_app(favorites_router)

    resp = await request(app, "DELETE", "/favorites/NOPE")

    assert resp.status_code == 404


async def test_get_favorites_mixed(fake_db, fake_redis):
    fake_db.fetchall_result = [("THYAO",), ("USD",), ("XAU-GRAM",)]
    app = build_app(favorites_router)

    resp = await request(app, "GET", "/favorites")

    assert resp.status_code == 200
    assert resp.json() == {"favorites": ["THYAO", "USD", "XAU-GRAM"]}


async def test_get_favorites_db_error(monkeypatch, fake_db, fake_redis):
    class _RaisingCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(favorites_module.db, "cursor", lambda *a, **k: _RaisingCursor())

    app = build_app(favorites_router)
    resp = await request(app, "GET", "/favorites")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "error_database"
