"""Unit tests for src/services/news.py.

Hermetic: the BigQuery client and the article fetcher are stubbed with canned
data, and Redis state lives in ``FakeRedis`` (the ``fake_redis`` fixture). No
BigQuery/network access happens.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import src.services.news as news_module
from src.models.article import Article


class FakeQueryJob:
    def __init__(self, df):
        self._df = df

    def result(self):
        return self

    def to_dataframe(self):
        return self._df


class FakeBigQueryClient:
    def __init__(self, df):
        self._df = df
        self.job_config = None

    def query(self, sql, job_config=None):
        self.sql = sql
        self.job_config = job_config
        return FakeQueryJob(self._df)


def _df(rows):
    return pd.DataFrame(rows, columns=["url", "title", "lang", "date"])


# ---------------------------------------------------------------------------
# clause builders
# ---------------------------------------------------------------------------


def test_build_title_clause_basic():
    c = news_module._build_title_clause(["THY"], None, None)
    assert "UPPER(title) LIKE '%THY%'" in c
    assert " OR " not in c


def test_build_title_clause_with_lang():
    c = news_module._build_title_clause(["THY"], None, ["TURKISH"])
    assert "UPPER(title) LIKE '%THY%'" in c
    assert "UPPER(lang) IN ('TURKISH')" in c


def test_build_title_clause_unsafe_terms_yield_false():
    assert news_module._build_title_clause(["TÜRK"], None, None) == "FALSE"
    assert news_module._build_title_clause([], None, None) == "FALSE"


def test_build_title_clause_filter_lang():
    filter_lang = {"THY": ["TURKISH"]}
    c = news_module._build_title_clause(["THY"], filter_lang, None)
    assert "UPPER(title) LIKE '%THY%'" in c
    assert "UPPER(lang) IN ('TURKISH')" in c

    c2 = news_module._build_title_clause(["THY"], filter_lang, ["EN"])
    assert c2 == "FALSE"


def test_build_gkg_clause():
    c = news_module._build_gkg_clause(["TURKISH AIRLINES"])
    assert "UPPER(V2Organizations) LIKE '%TURKISH AIRLINES%'" in c
    assert news_module._build_gkg_clause(["TÜRK"]) == "FALSE"
    assert news_module._build_gkg_clause([]) == "FALSE"


# ---------------------------------------------------------------------------
# search term resolution
# ---------------------------------------------------------------------------


def test_resolve_search_terms_mapping_hit(monkeypatch):
    monkeypatch.setattr(
        news_module,
        "_get_mapping",
        lambda: {"THYAO": {"name_tr": "Türk Hava", "search_title": ["THY"], "search_gkg": ["TURKISH AIRLINES"]}},
    )
    entry = news_module._resolve_search_terms("THYAO")
    assert entry["name_tr"] == "Türk Hava"
    assert entry["search_title"] == ["THY"]


def test_resolve_search_terms_fallback(monkeypatch):
    monkeypatch.setattr(news_module, "_get_mapping", lambda: {})
    entry = news_module._resolve_search_terms("UNKNOWN")
    assert entry["name_tr"] == "UNKNOWN"
    assert entry["search_title"] == ["UNKNOWN"]
    assert entry["search_gkg"] == ["UNKNOWN"]


# ---------------------------------------------------------------------------
# collect_articles (BigQuery stub, no network)
# ---------------------------------------------------------------------------


def _stub_client(monkeypatch, rows):
    client = FakeBigQueryClient(_df(rows))
    monkeypatch.setattr(news_module, "_get_client", lambda: client)
    monkeypatch.setattr(
        news_module,
        "_get_mapping",
        lambda: {"THYAO": {"name_tr": "THY", "search_title": ["THY"], "search_gkg": ["TURKISH AIRLINES"]}},
    )
    return client


async def test_collect_articles_builds_sql_and_parses_rows(monkeypatch):
    rows = [
        {"url": "http://a", "title": "Haber", "lang": "TR", "date": pd.Timestamp("2026-08-01T12:00:00")},
        {"url": "http://b", "title": None, "lang": "EN", "date": pd.Timestamp("2026-08-02T12:00:00")},
    ]
    client = _stub_client(monkeypatch, rows)

    articles = await news_module.collect_articles(
        "THYAO",
        from_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
        limit=5,
        lang=["TURKISH"],
    )

    assert len(articles) == 2
    assert articles[0].url == "http://a"
    assert articles[0].title == "Haber"
    assert articles[0].lang == "TR"
    assert articles[1].title == ""  # null title normalized to empty
    assert articles[1].lang == "EN"

    assert "gdelt-bq.gdeltv2.gqg" in client.sql
    assert "gdelt-bq.gdeltv2.gkg_partitioned" in client.sql
    assert "UPPER(title) LIKE '%THY%'" in client.sql
    assert "UPPER(V2Organizations) LIKE '%TURKISH AIRLINES%'" in client.sql
    assert len(client.job_config.query_parameters) == 2


async def test_collect_articles_repairs_mojibake_titles(monkeypatch):
    rows = [
        {"url": "http://a", "title": "Yeni yapýlandýrma", "lang": "TR", "date": pd.Timestamp("2026-08-01T12:00:00")},
    ]
    _stub_client(monkeypatch, rows)
    articles = await news_module.collect_articles("THYAO")
    assert articles[0].title == "Yeni yapılandırma"


async def test_collect_articles_diverse_tail_sql(monkeypatch):
    rows = [
        {"url": "http://a", "title": "t", "lang": "TR", "date": pd.Timestamp("2026-08-01T12:00:00")},
    ]
    client = _stub_client(monkeypatch, rows)
    await news_module.collect_articles("THYAO", diverse=True)
    assert "random_pick" in client.sql


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------


def test_serialize_articles_roundtrip():
    ts = datetime(2026, 8, 1, tzinfo=timezone.utc)
    arts = [Article(url="u1", title="t1", lang="TR", date=ts)]
    data = news_module._serialize_articles(arts)
    back = news_module._deserialize_articles(data)
    assert back[0].url == "u1"
    assert back[0].title == "t1"
    assert back[0].date == ts


# ---------------------------------------------------------------------------
# cache key
# ---------------------------------------------------------------------------


def test_cache_key_normalization():
    assert news_module._cache_key("thyao", 5) == "news:THYAO:10"
    assert news_module._cache_key("THYAO", 12) == "news:THYAO:20"
    assert news_module._cache_key("THYAO", 0) == "news:THYAO:1"


# ---------------------------------------------------------------------------
# get_latest_news
# ---------------------------------------------------------------------------


def _articles(n: int) -> list[Article]:
    return [
        Article(url=f"u{i}", title=f"t{i}", lang="TR", date=datetime.now(timezone.utc))
        for i in range(n)
    ]


async def test_get_latest_news_cache_hit(fake_redis):
    arts = _articles(3)
    fake_redis.store["news:THYAO:10"] = news_module._serialize_articles(arts)
    got = await news_module.get_latest_news("THYAO", 2)
    assert len(got) == 2
    assert got[0].url == "u0"
    assert got[1].url == "u1"


async def test_get_latest_news_miss_fetches_and_caches(fake_redis, monkeypatch):
    arts = _articles(5)

    async def _collect(query, from_date=None, limit=None, lang=None, diverse=False):
        return arts

    monkeypatch.setattr(news_module, "collect_articles", _collect)
    got = await news_module.get_latest_news("THYAO", 3)
    assert len(got) == 3
    cached = fake_redis.store.get("news:THYAO:10")
    assert cached is not None
    assert news_module._deserialize_articles(cached)[0].url == "u0"