"""Unit tests for src/services/search.py.

Hermetic: the BIST company cache (Redis-backed) and the on-disk mapping loader
are monkeypatched with canned data, so no Redis/network access happens. The
module-level dataset cache is reset before every test.
"""

import pytest

import src.services.search as search_module


@pytest.fixture(autouse=True)
def _reset_dataset(monkeypatch):
    monkeypatch.setattr(search_module, "_dataset", None)


def _mapping():
    return {
        "THYAO": {
            "name_tr": "TÜRK HAVA YOLLARI A.Ş.",
            "search_title": ["TÜRK HAVA", "TURKISH AIRLINES"],
        },
        "VAKBN": {
            "name_tr": "TÜRKİYE VAKIFLAR BANKASI T.A.O.",
            "search_title": ["VAKIFBANK"],
        },
        "GARAN": {
            "name_tr": "TÜRKİYE GARANTİ BANKASI A.Ş.",
            "search_title": ["GARANTI BANKASI"],
        },
        "XYZBN": {
            "name_tr": "XYZ HOLDING",
            "search_title": ["BANK", "XYZ"],
        },
    }


async def _companies():
    return [
        {
            "ticker": "THYAO",
            "name": "TÜRK HAVA YOLLARI",
            "city": "ISTANBUL",
            "auditor": "X",
            "company_id": "1",
        },
        {
            "ticker": "VAKBN",
            "name": "TÜRKİYE VAKIFLAR BANKASI",
            "city": "ISTANBUL",
            "auditor": "Y",
            "company_id": "2",
        },
    ]


def _stub_dataset(monkeypatch, companies=None):
    async def _co():
        return await (companies if companies is not None else _companies)()

    monkeypatch.setattr(search_module, "load_bist_mapping", _mapping)
    monkeypatch.setattr(search_module, "get_bist_companies_as_dict_from_redis", _co)


# ---------------------------------------------------------------------------
# _build_dataset
# ---------------------------------------------------------------------------


async def test_build_dataset_merges_pykap_and_mapping(monkeypatch):
    _stub_dataset(monkeypatch)
    ds = await search_module._build_dataset()

    assert ds["THYAO"]["name"] == "TÜRK HAVA YOLLARI"  # pykap wins
    assert ds["THYAO"]["city"] == "ISTANBUL"
    assert ds["THYAO"]["company_id"] == "1"
    assert ds["GARAN"]["name"] == "TÜRKİYE GARANTİ BANKASI A.Ş."  # mapping only
    assert ds["GARAN"]["city"] == ""


async def test_build_dataset_tolerates_redis_failure(monkeypatch):
    async def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(search_module, "load_bist_mapping", _mapping)
    monkeypatch.setattr(search_module, "get_bist_companies_as_dict_from_redis", _boom)
    ds = await search_module._build_dataset()
    assert "GARAN" in ds
    assert ds["GARAN"]["name"] == "TÜRKİYE GARANTİ BANKASI A.Ş."


async def test_get_dataset_caches(monkeypatch):
    _stub_dataset(monkeypatch)
    first = await search_module._get_dataset()
    second = await search_module._get_dataset()
    assert first is second


# ---------------------------------------------------------------------------
# search_companies scoring
# ---------------------------------------------------------------------------


async def test_search_exact_ticker_scores_100(monkeypatch):
    _stub_dataset(monkeypatch)
    res = await search_module.search_companies("THYAO")
    assert res[0]["ticker"] == "THYAO"
    assert res[0]["score"] == 100


async def test_search_alias_scores_95(monkeypatch):
    _stub_dataset(monkeypatch)
    res = await search_module.search_companies("VAKIFBANK")
    assert res[0]["ticker"] == "VAKBN"
    assert res[0]["score"] == 95


async def test_search_name_substring_scores_60(monkeypatch):
    _stub_dataset(monkeypatch)
    res = await search_module.search_companies("HAVA")
    assert any(r["ticker"] == "THYAO" and r["score"] == 60 for r in res)


async def test_search_search_title_scores_70(monkeypatch):
    _stub_dataset(monkeypatch)
    res = await search_module.search_companies("BANK")
    assert any(r["ticker"] == "XYZBN" and r["score"] == 70 for r in res)


async def test_search_alias_overrides_other_scores(monkeypatch):
    _stub_dataset(monkeypatch)
    res = await search_module.search_companies("TURKISH AIRLINES")
    # "TURKISH AIRLINES" is both an alias and a search_title; alias wins.
    assert any(r["ticker"] == "THYAO" and r["score"] == 95 for r in res)


async def test_search_empty_query_returns_empty(monkeypatch):
    _stub_dataset(monkeypatch)
    assert await search_module.search_companies("   ") == []


async def test_search_respects_limit(monkeypatch):
    _stub_dataset(monkeypatch)
    res = await search_module.search_companies("TURK", limit=1)
    assert len(res) == 1


async def test_search_sorted_by_score_then_ticker(monkeypatch):
    _stub_dataset(monkeypatch)
    res = await search_module.search_companies("TURK")
    scores = [r["score"] for r in res]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# _deduplicate
# ---------------------------------------------------------------------------


def test_deduplicate_keeps_highest_score():
    results = [
        {"ticker": "A", "name": "X", "score": 50},
        {"ticker": "B", "name": "X", "score": 90},
        {"ticker": "C", "name": "Y", "score": 40},
    ]
    deduped = search_module._deduplicate(results)
    assert len(deduped) == 2
    assert deduped[0]["ticker"] == "B"
    assert {r["name"] for r in deduped} == {"X", "Y"}