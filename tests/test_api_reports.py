"""Unit tests for src/api/reports.py.

Hermetic: the shared async ``db``/Redis singletons are swapped for in-memory
fakes and the external services (BIST ticker check, credits, report
generation, analytics) are stubbed so no network, DB, Redis or LLM call
happens. The feature-gate and job-slot dependencies are toggled by patching
the module-level functions they close over.
"""

from datetime import datetime, timezone

from src.api import reports as reports_module
from src.api.reports import router as reports_router
from src.core import job_slots as job_slots_module
from src.services import bist as bist_module
from src.services import maintenance as maintenance_module

from api_helpers import build_app, make_report, request


def _patch_generate_deps(monkeypatch, *, feature_enabled=True, slot_free=True, valid_ticker=True):
    async def _is_disabled(feature):
        return not feature_enabled

    async def _acquire(kind, user_id, ttl_seconds):
        return slot_free

    async def _release(kind, user_id, heartbeat=None):
        return None

    async def _heartbeat(kind, user_id):
        return None

    async def _valid(ticker):
        return valid_ticker

    monkeypatch.setattr(maintenance_module, "is_disabled", _is_disabled)
    monkeypatch.setattr(job_slots_module, "_acquire", _acquire)
    monkeypatch.setattr(job_slots_module, "_release", _release)
    monkeypatch.setattr(job_slots_module, "_heartbeat_loop", _heartbeat)
    monkeypatch.setattr(bist_module, "is_valid_bist_ticker", _valid)


def _patch_generate_services(monkeypatch, *, spend_ok=True, remaining=90.0):
    async def _spend(user_id, amount):
        return spend_ok, remaining

    async def _refund(user_id, amount):
        return None

    async def _credits(user_id):
        return remaining

    async def _gen(ticker, mode, user_id=None, purpose=None):
        return make_report()

    async def _track(*args, **kwargs):
        return None

    monkeypatch.setattr(reports_module, "credit_spend", _spend)
    monkeypatch.setattr(reports_module, "credit_refund", _refund)
    monkeypatch.setattr(reports_module, "get_credits", _credits)
    monkeypatch.setattr(reports_module, "generate_report", _gen)
    monkeypatch.setattr(reports_module, "track_event", _track)


# ---------------------------------------------------------------------------
# public info
# ---------------------------------------------------------------------------


async def test_report_info(monkeypatch, fake_db, fake_redis):
    app = build_app(reports_router)
    resp = await request(app, "GET", "/reports/info")

    assert resp.status_code == 200
    body = resp.json()
    assert body["quick_report"]["type"] == "quick_report"
    assert body["deep_report"]["type"] == "deep_report"
    assert body["token_cost_per_1k"] == 0.05


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


async def test_generate_success(monkeypatch, fake_db, fake_redis):
    _patch_generate_deps(monkeypatch)
    _patch_generate_services(monkeypatch)
    fake_db.fetchone_result = (101, datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))

    app = build_app(reports_router)
    resp = await request(
        app, "POST", "/reports/generate?ticker=THYAO&type=quick_report"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["report_id"] == 101
    assert body["about"] == "THYAO"
    assert body["title"] == "Test Raporu"
    assert body["remaining_credits"] == 90.0
    assert body["token_usage"]["total"] == 1000
    insert = [q for q in fake_db.queries if "INSERT INTO reports" in q[0]]
    assert len(insert) == 1
    assert fake_db.commit_calls >= 1


async def test_generate_feature_disabled(monkeypatch, fake_db, fake_redis):
    _patch_generate_deps(monkeypatch, feature_enabled=False)

    app = build_app(reports_router)
    resp = await request(
        app, "POST", "/reports/generate?ticker=THYAO&type=quick_report"
    )

    assert resp.status_code == 503
    assert "disabled for maintenance" in resp.json()["detail"]


async def test_generate_job_slot_busy(monkeypatch, fake_db, fake_redis):
    _patch_generate_deps(monkeypatch, slot_free=False)

    app = build_app(reports_router)
    resp = await request(
        app, "POST", "/reports/generate?ticker=THYAO&type=quick_report"
    )

    assert resp.status_code == 429
    assert "already running" in resp.json()["detail"]


async def test_generate_insufficient_credit(monkeypatch, fake_db, fake_redis):
    _patch_generate_deps(monkeypatch)
    _patch_generate_services(monkeypatch, spend_ok=False, remaining=0.0)

    app = build_app(reports_router)
    resp = await request(
        app, "POST", "/reports/generate?ticker=THYAO&type=quick_report"
    )

    assert resp.status_code == 402
    assert resp.json()["detail"] == "insufficient credit"


async def test_generate_invalid_type(monkeypatch, fake_db, fake_redis):
    _patch_generate_deps(monkeypatch)
    _patch_generate_services(monkeypatch)

    app = build_app(reports_router)
    resp = await request(
        app, "POST", "/reports/generate?ticker=THYAO&type=weekly_report"
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid type"


async def test_generate_invalid_ticker(monkeypatch, fake_db, fake_redis):
    _patch_generate_deps(monkeypatch, valid_ticker=False)

    app = build_app(reports_router)
    resp = await request(
        app, "POST", "/reports/generate?ticker=NOPE&type=quick_report"
    )

    assert resp.status_code == 404
    assert "Invalid BIST ticker" in resp.json()["detail"]


async def test_generate_missing_type(monkeypatch, fake_db, fake_redis):
    _patch_generate_deps(monkeypatch)
    _patch_generate_services(monkeypatch)

    app = build_app(reports_router)
    resp = await request(app, "POST", "/reports/generate?ticker=THYAO")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# history / search
# ---------------------------------------------------------------------------


def _history_rows():
    created_at = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    return [
        (1, "THYAO", "quick_report", "THYAO Analizi", '{"total": 100}', "bir amac", created_at),
        (2, "ASELS", "deep_report", "ASELS Analizi", None, None, created_at),
    ]


async def test_history_success(fake_db, fake_redis):
    fake_db.fetchall_result = _history_rows()

    app = build_app(reports_router)
    resp = await request(app, "GET", "/reports/history")

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert items[0]["id"] == 1
    assert items[0]["token_usage"] == {"total": 100}
    assert items[0]["purpose"] == "bir amac"
    assert items[1]["token_usage"] is None
    assert items[1]["title"] == "ASELS Analizi"


async def test_history_invalid_sort(fake_db, fake_redis):
    app = build_app(reports_router)
    resp = await request(app, "GET", "/reports/history?sort=banana")

    assert resp.status_code == 400
    assert "Invalid sort" in resp.json()["detail"]


async def test_history_invalid_order(fake_db, fake_redis):
    app = build_app(reports_router)
    resp = await request(app, "GET", "/reports/history?order=sideways")

    assert resp.status_code == 400
    assert "Invalid order" in resp.json()["detail"]


async def test_search_success(fake_db, fake_redis):
    fake_db.fetchall_result = _history_rows()

    app = build_app(reports_router)
    resp = await request(app, "GET", "/reports/search?q=THYAO")

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    select = [q for q in fake_db.queries if "ILIKE" in q[0]]
    assert len(select) == 1
    assert select[0][1][1] == "%THYAO%"


async def test_search_requires_query(fake_db, fake_redis):
    app = build_app(reports_router)
    resp = await request(app, "GET", "/reports/search")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# single report detail
# ---------------------------------------------------------------------------


async def test_get_single_report_success(fake_db, fake_redis):
    created_at = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    fake_db.fetchone_result = (
        "THYAO",
        "quick_report",
        "THYAO Analizi",
        '{"total": 1200}',
        "amac",
        "# Rapor",
        '[{"sentiment": "positive", "url": "http://x"}]',
        created_at,
    )

    app = build_app(reports_router)
    resp = await request(app, "GET", "/reports/1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["report_id"] == 1
    assert body["about"] == "THYAO"
    assert body["token_usage"] == {"total": 1200}
    assert body["sentiments"] == [{"sentiment": "positive", "url": "http://x"}]


async def test_get_single_report_not_found(fake_db, fake_redis):
    fake_db.fetchone_result = None

    app = build_app(reports_router)
    resp = await request(app, "GET", "/reports/999")

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


async def test_download_markdown(monkeypatch, fake_db, fake_redis):
    async def _get_by_id(report_id, user_id):
        return make_report()

    monkeypatch.setattr(reports_module, "get_report_by_id", _get_by_id)

    app = build_app(reports_router)
    resp = await request(app, "POST", "/reports/download?report_id=1&ftype=md")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "# Test Raporu" in resp.text


async def test_download_invalid_ftype(monkeypatch, fake_db, fake_redis):
    async def _get_by_id(report_id, user_id):
        return make_report()

    monkeypatch.setattr(reports_module, "get_report_by_id", _get_by_id)

    app = build_app(reports_router)
    resp = await request(app, "POST", "/reports/download?report_id=1&ftype=exe")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid file type."


async def test_download_report_not_found(monkeypatch, fake_db, fake_redis):
    async def _missing(report_id, user_id):
        return None

    monkeypatch.setattr(reports_module, "get_report_by_id", _missing)

    app = build_app(reports_router)
    resp = await request(app, "POST", "/reports/download?report_id=999&ftype=md")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Report not found."