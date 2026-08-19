"""Unit tests for src/api/exports.py.

Hermetic: the shared async ``db``/Redis singletons are swapped for in-memory
fakes and the background export worker (``_run_export``) is stubbed so no
real database, Redis, filesystem export or mail call happens.
"""

from datetime import datetime, timedelta, timezone

from src.api import exports as exports_module
from src.api.exports import router as exports_router

from api_helpers import build_app, request

NOW = datetime.now(timezone.utc)
FUTURE = NOW + timedelta(days=7)


def _export_row(**overrides):
    row = (
        1,        # id
        7,        # user_id
        2025,     # year
        "csv",    # format
        "ready",  # status
        "/tmp/nonexistent.gz",  # file_path
        "tok123",  # token
        FUTURE,   # expires_at
        1000,     # row_count
        2048,     # size_bytes
        0,        # downloaded_count
        None,     # error
        NOW,      # created_at
        NOW,      # updated_at
    )
    if overrides:
        row = list(row)
        for idx, key in enumerate(
            (
                "id", "user_id", "year", "format", "status", "file_path", "token",
                "expires_at", "row_count", "size_bytes", "downloaded_count",
                "error", "created_at", "updated_at",
            )
        ):
            if key in overrides:
                row[idx] = overrides[key]
        row = tuple(row)
    return row


def _patch_worker(monkeypatch):
    async def _no_run_export(export_id):
        return None

    monkeypatch.setattr(exports_module, "_run_export", _no_run_export)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_export_success(monkeypatch, fake_db, fake_redis):
    _patch_worker(monkeypatch)
    fake_db.queue_fetchone(None, (1,))

    app = build_app(exports_router)
    resp = await request(
        app, "POST", "/data/export", json={"year": 2025, "format": "csv"}
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body == {"export_id": 1, "status": "queued"}
    inserts = [q for q in fake_db.queries if "INSERT INTO exports" in q[0]]
    assert len(inserts) == 1


async def test_create_export_idempotent(monkeypatch, fake_db, fake_redis):
    _patch_worker(monkeypatch)
    fake_db.queue_fetchone((5, "ready"))

    app = build_app(exports_router)
    resp = await request(
        app, "POST", "/data/export", json={"year": 2025, "format": "csv"}
    )

    assert resp.status_code == 202
    assert resp.json() == {"export_id": 5, "status": "ready"}
    inserts = [q for q in fake_db.queries if "INSERT INTO exports" in q[0]]
    assert len(inserts) == 0


async def test_create_export_invalid_year(monkeypatch, fake_db, fake_redis):
    _patch_worker(monkeypatch)

    app = build_app(exports_router)
    resp = await request(
        app, "POST", "/data/export", json={"year": 1980, "format": "csv"}
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid year"


async def test_create_export_invalid_format(monkeypatch, fake_db, fake_redis):
    _patch_worker(monkeypatch)

    app = build_app(exports_router)
    resp = await request(
        app, "POST", "/data/export", json={"year": 2025, "format": "xml"}
    )

    assert resp.status_code == 422


async def test_create_export_missing_body(monkeypatch, fake_db, fake_redis):
    _patch_worker(monkeypatch)

    app = build_app(exports_router)
    resp = await request(app, "POST", "/data/export", json={})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_exports(fake_db, fake_redis):
    fake_db.fetchall_result = [
        _export_row(id=1, status="ready"),
        _export_row(id=2, status="queued", token=None, expires_at=None),
    ]

    app = build_app(exports_router)
    resp = await request(app, "GET", "/data/export")

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert items[0]["downloadable"] is True
    assert items[0]["download_url"] == "/api/v1/data/export/download/tok123"
    assert items[0]["format"] == "csv"
    assert items[1]["downloadable"] is False
    assert items[1]["download_url"] is None


# ---------------------------------------------------------------------------
# single export
# ---------------------------------------------------------------------------


async def test_get_export_success(fake_db, fake_redis):
    fake_db.fetchone_result = _export_row()

    app = build_app(exports_router)
    resp = await request(app, "GET", "/data/export/1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    assert body["year"] == 2025
    assert body["status"] == "ready"


async def test_get_export_not_found(fake_db, fake_redis):
    fake_db.fetchone_result = None

    app = build_app(exports_router)
    resp = await request(app, "GET", "/data/export/999")

    assert resp.status_code == 404


async def test_get_export_not_owner(fake_db, fake_redis):
    fake_db.fetchone_result = _export_row(user_id=99)

    app = build_app(exports_router, user_id=7)
    resp = await request(app, "GET", "/data/export/1")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# public download
# ---------------------------------------------------------------------------


def _download_row(**overrides):
    """Row shape expected by the public download endpoint (6 columns)."""
    row = (
        1,        # id
        2025,     # year
        "csv",    # format
        "ready",  # status
        FUTURE,   # expires_at
        "/tmp/nonexistent.gz",  # file_path
    )
    if overrides:
        row = list(row)
        for idx, key in enumerate(("id", "year", "format", "status", "expires_at", "file_path")):
            if key in overrides:
                row[idx] = overrides[key]
        row = tuple(row)
    return row


async def test_download_export_success(fake_db, fake_redis, tmp_path):
    target = tmp_path / "florence.gz"
    target.write_bytes(b"dummy gzip payload")
    fake_db.fetchone_result = _download_row(file_path=str(target), status="ready")

    app = build_app(exports_router)
    resp = await request(app, "GET", "/data/export/download/tok123")

    assert resp.status_code == 200
    assert resp.content == b"dummy gzip payload"
    assert "florence-daily-2025.csv.gz" in resp.headers["content-disposition"]


async def test_download_export_not_found(fake_db, fake_redis):
    fake_db.fetchone_result = None

    app = build_app(exports_router)
    resp = await request(app, "GET", "/data/export/download/bogus")

    assert resp.status_code == 404


async def test_download_export_not_ready(fake_db, fake_redis):
    fake_db.fetchone_result = _download_row(status="queued")

    app = build_app(exports_router)
    resp = await request(app, "GET", "/data/export/download/tok123")

    assert resp.status_code == 410


async def test_download_export_expired(fake_db, fake_redis):
    fake_db.fetchone_result = _download_row(expires_at=NOW - timedelta(days=1))

    app = build_app(exports_router)
    resp = await request(app, "GET", "/data/export/download/tok123")

    assert resp.status_code == 410


async def test_download_export_file_missing(fake_db, fake_redis):
    fake_db.fetchone_result = _download_row(status="ready", file_path="/tmp/does/not/exist.gz")

    app = build_app(exports_router)
    resp = await request(app, "GET", "/data/export/download/tok123")

    assert resp.status_code == 404