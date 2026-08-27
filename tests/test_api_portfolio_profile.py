"""Unit tests for src/api/portfolio.py (``POST /portfolio/profile``).

Bu endpoint portfoy alim-satimiyla ilgili degil (o kisim
``src/api/virtual_portfolio.py``'de, ayrica test edilir) -- portfoydeki
tikerlardan bir "yatirimci profili" tahmin eden ve benzer hisseler oneren
salt-okunur bir advisor uc noktasi. Tum dis bagimliliklar (Redis vektor
onbellegi, sirket bilgisi servisi, popular ticker listesi) stub'lanir; hic
gercek DB/Redis/ag erisimi olmaz.
"""

import src.api.portfolio as portfolio_api
from src.services import company as company_module
from src.services import stats as stats_module

from api_helpers import build_app, request


def _patch(monkeypatch, **funcs):
    for name, func in funcs.items():
        monkeypatch.setattr(portfolio_api, name, func)


def _default_stubs(monkeypatch, vectors=None, candidates=None, candidate_vectors=None):
    """Portfoy profili icin makul varsayilanlarla stub kurar.

    ``vectors``: {ticker: [risk, horizon, profitability] | None} -- redis'te
    hazir bulunan (veya bulunmayan) vektorler.
    ``candidates``: get_popular_tickers'in donecegi aday ticker listesi.
    ``candidate_vectors``: adaylar icin ayni sekilde {ticker: vector | None}.
    """
    vectors = vectors or {}
    candidates = candidates if candidates is not None else []
    candidate_vectors = candidate_vectors or {}

    async def _read_vectors(tickers):
        # Hem body.tickers hem candidate listesi icin cagrilir; hangi
        # kumeden geldigine gore uygun sozlukten okur.
        out = {}
        for t in tickers:
            if t in vectors:
                out[t] = vectors[t]
            elif t in candidate_vectors:
                out[t] = candidate_vectors[t]
            else:
                out[t] = None
        return out

    written = []

    async def _write_vectors(items):
        written.append(items)

    async def _get_popular(n):
        return candidates

    async def _get_company_info(ticker, use_cache=True):
        return None  # varsayilan: sirket bilgisi yok -> eksik ticker'lar 400'e duser

    _patch(
        monkeypatch,
        read_vectors_from_redis=_read_vectors,
        write_vectors_to_redis=_write_vectors,
    )
    monkeypatch.setattr(stats_module, "get_popular_tickers", _get_popular)
    monkeypatch.setattr(company_module, "get_company_info", _get_company_info)
    return written


# ---------------------------------------------------------------------------
# Mutlu yol
# ---------------------------------------------------------------------------


async def test_portfolio_profile_success_with_cached_vectors(monkeypatch, fake_redis):
    _default_stubs(
        monkeypatch,
        vectors={"THYAO": [0.6, 0.5, 0.4]},
        candidates=["AKBNK", "GARAN"],
        candidate_vectors={"AKBNK": [0.5, 0.5, 0.5], "GARAN": [0.9, 0.1, 0.1]},
    )

    app = build_app(portfolio_api.router)
    resp = await request(app, "POST", "/portfolio/profile", json={"tickers": ["thyao"], "limit": 5})

    assert resp.status_code == 200
    body = resp.json()
    assert body["portfolio"] == [{"ticker": "THYAO", "vector": [0.6, 0.5, 0.4]}]
    assert body["avg_vector"] == [0.6, 0.5, 0.4]
    # iki adaydan da skor uretilmis olmali, en yakin (AKBNK) once gelir.
    tickers_scored = [s["ticker"] for s in body["similar_stocks"]]
    assert set(tickers_scored) == {"AKBNK", "GARAN"}
    assert tickers_scored[0] == "AKBNK"


async def test_portfolio_profile_uppercases_tickers(monkeypatch, fake_redis):
    _default_stubs(monkeypatch, vectors={"THYAO": [0.5, 0.5, 0.5]})

    app = build_app(portfolio_api.router)
    resp = await request(app, "POST", "/portfolio/profile", json={"tickers": ["thyao"]})

    assert resp.status_code == 200
    assert resp.json()["portfolio"][0]["ticker"] == "THYAO"


async def test_portfolio_profile_limit_caps_similar_stocks(monkeypatch, fake_redis):
    _default_stubs(
        monkeypatch,
        vectors={"THYAO": [0.5, 0.5, 0.5]},
        candidates=["A", "B", "C"],
        candidate_vectors={
            "A": [0.5, 0.5, 0.5],
            "B": [0.1, 0.1, 0.1],
            "C": [0.9, 0.9, 0.9],
        },
    )

    app = build_app(portfolio_api.router)
    resp = await request(
        app, "POST", "/portfolio/profile", json={"tickers": ["THYAO"], "limit": 1}
    )

    assert resp.status_code == 200
    assert len(resp.json()["similar_stocks"]) == 1


async def test_portfolio_profile_excludes_own_tickers_from_candidates(monkeypatch, fake_redis):
    """Verilen portfoydeki bir ticker aday havuzunda da varsa onerilerden cikarilmali."""
    _default_stubs(
        monkeypatch,
        vectors={"THYAO": [0.5, 0.5, 0.5]},
        candidates=["THYAO", "AKBNK"],
        candidate_vectors={"THYAO": [0.5, 0.5, 0.5], "AKBNK": [0.4, 0.4, 0.4]},
    )

    app = build_app(portfolio_api.router)
    resp = await request(app, "POST", "/portfolio/profile", json={"tickers": ["THYAO"]})

    assert resp.status_code == 200
    tickers_scored = [s["ticker"] for s in resp.json()["similar_stocks"]]
    assert "THYAO" not in tickers_scored
    assert tickers_scored == ["AKBNK"]


# ---------------------------------------------------------------------------
# Eksik vektor -> sirket bilgisinden hesapla + redis'e yaz
# ---------------------------------------------------------------------------


async def test_portfolio_profile_fetches_missing_vector_from_company_info(monkeypatch, fake_redis):
    written = _default_stubs(monkeypatch, vectors={"THYAO": None})

    async def _company_info(ticker, use_cache=True):
        assert ticker == "THYAO"
        return {"trading": {"beta": 1.0, "averageVolume": 1_000_000}, "market": {"currentPrice": 50}}

    monkeypatch.setattr(company_module, "get_company_info", _company_info)

    app = build_app(portfolio_api.router)
    resp = await request(app, "POST", "/portfolio/profile", json={"tickers": ["THYAO"]})

    assert resp.status_code == 200
    assert resp.json()["portfolio"][0]["ticker"] == "THYAO"
    # Yeni hesaplanan vektor redis'e yazilmis olmali.
    assert any(item["ticker"] == "THYAO" for batch in written for item in batch)


async def test_portfolio_profile_fetches_missing_candidate_vector_from_company_info(monkeypatch, fake_redis):
    """Verilen portfoydeki tickerlar degil, ADAY havuzundaki eksik vektorler
    icin de ayni company-info fallback yolu calismali (satir 73-81)."""
    _default_stubs(
        monkeypatch,
        vectors={"THYAO": [0.5, 0.5, 0.5]},
        candidates=["AKBNK"],
        candidate_vectors={"AKBNK": None},  # aday icin redis'te vektor yok
    )

    async def _company_info(ticker, use_cache=True):
        assert ticker == "AKBNK"
        return {"trading": {"beta": 1.2, "averageVolume": 2_000_000}, "market": {"currentPrice": 30}}

    monkeypatch.setattr(company_module, "get_company_info", _company_info)

    app = build_app(portfolio_api.router)
    resp = await request(app, "POST", "/portfolio/profile", json={"tickers": ["THYAO"]})

    assert resp.status_code == 200
    tickers_scored = [s["ticker"] for s in resp.json()["similar_stocks"]]
    assert tickers_scored == ["AKBNK"]  # sirket bilgisinden hesaplanan vektorle oneriye girdi


async def test_portfolio_profile_no_vector_data_returns_400(monkeypatch, fake_redis):
    _default_stubs(monkeypatch, vectors={"THYAO": None})  # company_info varsayilani None doner

    app = build_app(portfolio_api.router)
    resp = await request(app, "POST", "/portfolio/profile", json={"tickers": ["THYAO"]})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "No vector data available for given tickers"


# ---------------------------------------------------------------------------
# Girdi dogrulama (pydantic field_validator'lar)
# ---------------------------------------------------------------------------


async def test_portfolio_profile_rejects_empty_tickers(monkeypatch, fake_redis):
    _default_stubs(monkeypatch)

    app = build_app(portfolio_api.router)
    resp = await request(app, "POST", "/portfolio/profile", json={"tickers": []})

    assert resp.status_code == 422


async def test_portfolio_profile_rejects_too_many_tickers(monkeypatch, fake_redis):
    _default_stubs(monkeypatch)

    app = build_app(portfolio_api.router)
    resp = await request(
        app, "POST", "/portfolio/profile", json={"tickers": [f"T{i}" for i in range(51)]}
    )

    assert resp.status_code == 422


async def test_portfolio_profile_rejects_limit_out_of_range(monkeypatch, fake_redis):
    _default_stubs(monkeypatch, vectors={"THYAO": [0.5, 0.5, 0.5]})

    app = build_app(portfolio_api.router)

    too_low = await request(
        app, "POST", "/portfolio/profile", json={"tickers": ["THYAO"], "limit": 0}
    )
    assert too_low.status_code == 422

    too_high = await request(
        app, "POST", "/portfolio/profile", json={"tickers": ["THYAO"], "limit": 51}
    )
    assert too_high.status_code == 422


# ---------------------------------------------------------------------------
# Bakim kapisi (require_feature("advisor"))
# ---------------------------------------------------------------------------


async def test_portfolio_profile_503_when_advisor_disabled(monkeypatch, fake_redis):
    _default_stubs(monkeypatch, vectors={"THYAO": [0.5, 0.5, 0.5]})
    await fake_redis.sadd("maintenance:disabled", "advisor")

    app = build_app(portfolio_api.router)
    resp = await request(app, "POST", "/portfolio/profile", json={"tickers": ["THYAO"]})

    assert resp.status_code == 503
