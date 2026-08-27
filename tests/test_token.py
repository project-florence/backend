"""Unit tests for src/services/token.py -- REFACTOR_PLAN.md Adim 3.

Hermetic: ``fake_db`` swaps the shared async ``db`` singleton (see
tests/conftest.py, tests/api_helpers.py). Covers:

1. ``log_token_usage`` -- new columns (purpose/provider/status/error/
   duration_ms) reach the INSERT; ``endpoint`` defaults to ``purpose`` when
   not given explicitly (backward compat for old endpoint-filtered queries).
2. ``get_token_summary`` -- unchanged aggregate shape when ``group_by`` is
   omitted; new ``status``/``purpose``/``provider``/``model`` filters; new
   ``group_by`` breakdown (allowlisted, REJECTS an unknown value instead of
   interpolating it into SQL -- src/api/reports.py::_sort_order_clause
   deseni).
"""

import pytest
from fastapi import HTTPException

from src.services.token import get_token_summary, log_token_usage


# ---------------------------------------------------------------------------
# log_token_usage
# ---------------------------------------------------------------------------


async def test_log_token_usage_success_row_shape(fake_db):
    await log_token_usage(
        model="deepseek-v4-flash-free",
        purpose="digest",
        provider="opencode-zen",
        status="ok",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        duration_ms=500,
        user_id=3,
    )

    inserts = [q for q in fake_db.queries if "INSERT INTO token_usage" in q[0]]
    assert len(inserts) == 1
    _, params = inserts[0]
    (
        model,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        endpoint,
        purpose,
        provider,
        status,
        error,
        duration_ms,
        user_id,
        created_at,
    ) = params
    assert model == "deepseek-v4-flash-free"
    assert (prompt_tokens, completion_tokens, total_tokens) == (10, 20, 30)
    assert endpoint == "digest"  # endpoint acikca verilmedi -> purpose ile ayni
    assert purpose == "digest"
    assert provider == "opencode-zen"
    assert status == "ok"
    assert error is None
    assert duration_ms == 500
    assert user_id == 3
    assert created_at is not None
    assert fake_db.commit_calls == 1


async def test_log_token_usage_failure_row_allows_null_token_counts(fake_db):
    await log_token_usage(
        model="gpt-5",
        purpose="report",
        provider="openai",
        status="error",
        error="RuntimeError: 400 please use low, high, or max",
        duration_ms=120,
    )

    _, params = fake_db.queries[0]
    assert params[1] is None and params[2] is None and params[3] is None  # token sayilari
    assert params[7] == "error"
    assert params[8] == "RuntimeError: 400 please use low, high, or max"


async def test_log_token_usage_explicit_endpoint_overrides_purpose(fake_db):
    """Gecmis cagri yeri (ornegin farkli bir endpoint adi istenirse) hala
    mumkun -- purpose ile ayni deger ZORUNLU degil, sadece varsayilan."""
    await log_token_usage(
        model="m",
        purpose="report",
        endpoint="legacy-report-endpoint",
        status="ok",
    )
    _, params = fake_db.queries[0]
    assert params[4] == "legacy-report-endpoint"
    assert params[5] == "report"


# ---------------------------------------------------------------------------
# get_token_summary -- backward-compatible aggregate shape
# ---------------------------------------------------------------------------


async def test_get_token_summary_without_group_by_returns_aggregate_only(fake_db):
    fake_db.fetchone_result = (5, 100, 200, 300)

    result = await get_token_summary()

    assert result == {
        "call_count": 5,
        "total_prompt_tokens": 100,
        "total_completion_tokens": 200,
        "total_tokens": 300,
    }
    assert "breakdown" not in result


async def test_get_token_summary_status_filter_adds_where_clause(fake_db):
    fake_db.fetchone_result = (1, 0, 0, 0)

    await get_token_summary(status="error")

    query, params = fake_db.queries[0]
    assert "status = %s" in query
    assert "error" in params


async def test_get_token_summary_purpose_provider_model_filters(fake_db):
    fake_db.fetchone_result = (1, 0, 0, 0)

    await get_token_summary(purpose="digest", provider="opencode-zen", model="deepseek-v4-flash-free")

    query, params = fake_db.queries[0]
    assert "purpose = %s" in query
    assert "provider = %s" in query
    assert "model = %s" in query
    assert params == ["digest", "opencode-zen", "deepseek-v4-flash-free"]


# ---------------------------------------------------------------------------
# get_token_summary -- group_by kirilimi
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group_by", ["provider", "model", "purpose"])
async def test_get_token_summary_group_by_allowed_values(fake_db, group_by):
    fake_db.fetchone_result = (10, 1000, 2000, 3000)
    fake_db.fetchall_result = [
        ("a", 6, 600, 1200, 1800),
        ("b", 4, 400, 800, 1200),
    ]

    result = await get_token_summary(group_by=group_by)

    assert result["call_count"] == 10
    assert "breakdown" in result
    assert len(result["breakdown"]) == 2
    first = result["breakdown"][0]
    assert first == {
        "group_by": group_by,
        "value": "a",
        "call_count": 6,
        "total_prompt_tokens": 600,
        "total_completion_tokens": 1200,
        "total_tokens": 1800,
    }
    # GROUP BY kolonu allowlist'ten geldigi icin sorguda gercekten yer almali.
    group_query = [q for q in fake_db.queries if "GROUP BY" in q[0]][0][0]
    assert f"GROUP BY {group_by}" in group_query


async def test_get_token_summary_group_by_rejects_unknown_value(fake_db):
    """Kullanici girdisi (admin endpoint query param) dogrudan SQL'e
    ulasiyor -- allowlist disindaki bir deger SQL'e hic gitmeden 400
    dondurmeli (src/api/reports.py::_sort_order_clause deseni)."""
    fake_db.fetchone_result = (0, 0, 0, 0)

    with pytest.raises(HTTPException) as exc_info:
        await get_token_summary(group_by="'; DROP TABLE token_usage; --")

    assert exc_info.value.status_code == 400
    # Ikinci (breakdown) sorgusu hic calismamali.
    assert not any("GROUP BY" in q[0] for q in fake_db.queries)
