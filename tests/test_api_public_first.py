"""B-17 — anonim piyasa okumasi (public-first) + IP bazli rate limit testleri.

Iki katman birlikte dogrulanir:

1. ``auth_and_tracking_middleware`` (src/main.py) dogrudan cagrilir: public
   okuma yollari token'siz 200 alir, kisisel yollar 401 alir, yazma metotlari
   (POST) public yolda bile auth ister, anonim IP limiti 429 + ``Retry-After``
   dondirir ve farkli IP'ler bagimsizdir.
2. Tam router + middleware'den kurulu kucuk bir ASGI uygulamasi ile asil
   handler'larin anonime 200 dondurdugu dogrulanir (digest ve news handler'lari
   artik ``get_current_user_optional`` / ``get_current_user_full_optional``
   kullanir; yalnizca middleware'in public saymasi yetmez).

Testler hermetik: gercek DB/Redis/ag yok; servisler monkeypatch'lenir.
"""

import json
from datetime import date, datetime, timezone

import jwt
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

import src.main as main_module
import src.api.bist as bist_api
import src.api.ipo as ipo_api
import src.services.bist as bist_service
from src.api.deps import ALGORITHM, SECRET_KEY
from src.api.router import router as api_router
from src.finance import finance_service
from src.services.digest import reads as digest_reads
from src.services.digest.models import Digest

from api_helpers import request as http_request

middleware = main_module.auth_and_tracking_middleware

# Anonim GET ile 200 beklenen public okuma uclari (B-17 listesi).
PUBLIC_READ_URLS = [
    "/api/v1/companies/summary",
    "/api/v1/companies/info/THYAO",
    "/api/v1/companies/search?query=thy",
    "/api/v1/price/current?ticker=THYAO",
    "/api/v1/price/history/THYAO",
    "/api/v1/economy/quotes",
    "/api/v1/economy/history/USD",
    "/api/v1/ipos/upcoming",
    "/api/v1/ipos/draft",
    "/api/v1/ipos/active",
    "/api/v1/ipos/some-slug",
    "/api/v1/news/THYAO",
    "/api/v1/digest",
]

# Anonime 401 donmesi gereken kisisel uclar.
PERSONAL_URLS = [
    "/api/v1/favorites",
    "/api/v1/portfolios",
    "/api/v1/reports/history",
    "/api/v1/credits",
    "/api/v1/profile",
]


# ---------------------------------------------------------------------------
# Middleware dogrudan cagri yardimcilari (test_main_middleware.py kalibi)
# ---------------------------------------------------------------------------


def _make_request(path, method="GET", token=None, forwarded_for=None, client_ip="9.9.9.9"):
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "query_string": b"",
        "client": (client_ip, 1234),
    }
    return Request(scope)


def _make_token(user_id=7, iat=None, exp=None):
    now = datetime.now(timezone.utc)
    payload = {"user_id": user_id, "iat": int((iat or now).timestamp())}
    if exp is not None:
        payload["exp"] = int(exp.timestamp())
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


class _CallNextSpy:
    def __init__(self, response=None):
        self.called = False
        self.received_request = None
        self._response = response or Response("ok", status_code=200)

    async def __call__(self, request):
        self.called = True
        self.received_request = request
        return self._response


# ---------------------------------------------------------------------------
# Middleware: public okuma yollari anonime gecer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_READ_URLS)
async def test_public_read_path_passes_without_token(path, fake_db, fake_redis):
    call_next = _CallNextSpy()
    response = await middleware(_make_request(path), call_next)

    assert response.status_code == 200
    assert call_next.called is True


@pytest.mark.parametrize("path", PERSONAL_URLS)
async def test_personal_path_401_for_anonymous(path, fake_db, fake_redis):
    call_next = _CallNextSpy()
    response = await middleware(_make_request(path), call_next)

    assert response.status_code == 401
    assert json.loads(response.body)["detail"] == "Not authenticated"
    assert call_next.called is False


@pytest.mark.parametrize(
    "path",
    ["/api/v1/companies/summary", "/api/v1/digest", "/api/v1/news/THYAO"],
)
async def test_public_read_path_post_still_requires_auth(path, fake_db, fake_redis):
    """Public yol yalniz GET icin; POST ayni path'te auth ister."""
    call_next = _CallNextSpy()
    response = await middleware(_make_request(path, method="POST"), call_next)

    assert response.status_code == 401
    assert call_next.called is False


async def test_prefix_match_does_not_open_personal_siblings(fake_db, fake_redis):
    """'/api/v1/companies/info' public olsa da kardes kisisel uclar acilmaz.

    Ayni sekilde '/api/v1/economy/quotes' public iken '/api/v1/economy/records'
    ve '/api/v1/economy/analysis/USD' auth'lu kalir.
    """
    for path in ("/api/v1/economy/records", "/api/v1/economy/analysis/USD",
                 "/api/v1/economy/providers", "/api/v1/companies/info"):
        call_next = _CallNextSpy()
        response = await middleware(_make_request(path), call_next)
        if path == "/api/v1/companies/info":
            assert response.status_code == 200
        else:
            assert response.status_code == 401, path


# ---------------------------------------------------------------------------
# Middleware: anonim IP rate limiti
# ---------------------------------------------------------------------------


async def test_anonymous_news_ip_limit_429_with_retry_after(fake_db, fake_redis, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    for _ in range(10):
        call_next = _CallNextSpy()
        response = await middleware(_make_request("/api/v1/news/THYAO"), call_next)
        assert response.status_code == 200

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/news/THYAO"), call_next)

    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "60"
    assert json.loads(response.body)["detail"] == "error_rate_limited"
    assert call_next.called is False


async def test_anonymous_limits_are_independent_per_ip(fake_db, fake_redis, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    for _ in range(10):
        assert (await middleware(
            _make_request("/api/v1/news/THYAO", forwarded_for="1.1.1.1"), _CallNextSpy()
        )).status_code == 200

    # Ikinci IP kendi kovasinda sifirdan baslar.
    assert (await middleware(
        _make_request("/api/v1/news/THYAO", forwarded_for="2.2.2.2"), _CallNextSpy()
    )).status_code == 200

    # Ilk IP tavani asti.
    over = await middleware(
        _make_request("/api/v1/news/THYAO", forwarded_for="1.1.1.1"), _CallNextSpy()
    )
    assert over.status_code == 429


async def test_price_endpoint_60_per_minute_then_429(fake_db, fake_redis, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    for _ in range(60):
        assert (await middleware(
            _make_request("/api/v1/price/current?ticker=THYAO", forwarded_for="3.3.3.3"),
            _CallNextSpy(),
        )).status_code == 200

    over = await middleware(
        _make_request("/api/v1/price/current?ticker=THYAO", forwarded_for="3.3.3.3"), _CallNextSpy()
    )
    assert over.status_code == 429

    # Farkli public okuma uclarinin kendi kovasi vardir.
    assert (await middleware(
        _make_request("/api/v1/companies/summary", forwarded_for="3.3.3.3"), _CallNextSpy()
    )).status_code == 200


async def test_trust_proxy_headers_disabled_ignores_forwarded_for(fake_db, fake_redis, monkeypatch):
    """TRUST_PROXY_HEADERS=0 iken sahte X-Forwarded-For limit atlatamaz:
    peer adresi kullanilir, bu yuzden farkli XFF'ler ayni kovayi paylasir."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "0")
    for i in range(10):
        assert (await middleware(
            _make_request("/api/v1/news/THYAO", forwarded_for=f"10.0.0.{i}", client_ip="5.5.5.5"),
            _CallNextSpy(),
        )).status_code == 200

    over = await middleware(
        _make_request("/api/v1/news/THYAO", forwarded_for="10.0.0.99", client_ip="5.5.5.5"),
        _CallNextSpy(),
    )
    assert over.status_code == 429


async def test_authenticated_user_not_subject_to_ip_limit(fake_db, fake_redis, monkeypatch):
    """Gecerli token'li istek anonim IP kovasina yazilmaz (per-user davranis)."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    # password_changed_at=NULL, is_frozen=False; sonrasi Redis cache'inden gelir.
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=42)

    for _ in range(15):
        call_next = _CallNextSpy()
        response = await middleware(
            _make_request("/api/v1/companies/summary", token=token), call_next
        )
        assert response.status_code == 200
        assert call_next.called is True

    assert "ratelimit:anon:/api/v1/companies/summary:9.9.9.9" not in fake_redis.store


# ---------------------------------------------------------------------------
# Handler seviyesi (tam router + middleware): anonime 200 / kisisel 401
# ---------------------------------------------------------------------------


def _build_public_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(middleware)
    app.include_router(api_router)
    return app


@pytest.fixture
def patched_public_reads(monkeypatch):
    """Public okuma uclarindaki tum servis bagimliliklarini sabitler."""
    async def _summary(**kwargs):
        return {"items": [], "as_of": None}

    async def _company_info(ticker):
        return {"ticker": ticker}

    async def _search(query):
        return []

    async def _ticker_valid(ticker):
        return True

    async def _quote(ticker):
        return {"ticker": ticker, "price": 10.0}

    async def _history(ticker, period, interval):
        return {"candles": []}

    async def _news(ticker, amount):
        return []

    async def _stat(ticker, kind):
        return None

    async def _economy_quotes(wanted=None):
        return {"quotes": {}}

    async def _candles(symbol, interval, start, end):
        return []

    async def _json_list(**kwargs):
        return "[]"

    async def _json_detail(slug):
        return "{}"

    async def _current_digest():
        return Digest(date=date(2026, 8, 19), slot="morning", title="t", content="c")

    monkeypatch.setattr(bist_api, "get_companies_summary", _summary)
    monkeypatch.setattr(bist_api, "get_company_info", _company_info)
    monkeypatch.setattr(bist_api, "search_companies_by_text", _search)
    monkeypatch.setattr(bist_api, "get_quote", _quote)
    monkeypatch.setattr(bist_api, "get_price_history", _history)
    monkeypatch.setattr(bist_api, "get_latest_news", _news)
    monkeypatch.setattr(bist_api, "increment_stat", _stat)
    monkeypatch.setattr(bist_service, "is_valid_bist_ticker", _ticker_valid)
    monkeypatch.setattr(finance_service, "get_quotes", _economy_quotes)
    monkeypatch.setattr(finance_service, "get_candles", _candles)
    monkeypatch.setattr(ipo_api, "get_upcoming_ipos", _json_list)
    monkeypatch.setattr(ipo_api, "get_draft_ipos", _json_list)
    monkeypatch.setattr(ipo_api, "get_active_ipos", _json_list)
    monkeypatch.setattr(ipo_api, "get_ipo_detail_by_slug", _json_detail)
    monkeypatch.setattr(digest_reads, "get_current_digest", _current_digest)


@pytest.mark.parametrize("url", PUBLIC_READ_URLS)
async def test_anonymous_handler_returns_200(url, fake_db, fake_redis, patched_public_reads):
    app = _build_public_app()
    resp = await http_request(app, "GET", url)

    assert resp.status_code == 200, f"{url} -> {resp.status_code}: {resp.text}"


@pytest.mark.parametrize("url", PERSONAL_URLS)
async def test_anonymous_handler_personal_returns_401(url, fake_db, fake_redis):
    app = _build_public_app()
    resp = await http_request(app, "GET", url)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


async def test_anonymous_news_limit_enforced_end_to_end(fake_db, fake_redis, patched_public_reads):
    app = _build_public_app()
    for _ in range(10):
        resp = await http_request(app, "GET", "/api/v1/news/THYAO")
        assert resp.status_code == 200

    resp = await http_request(app, "GET", "/api/v1/news/THYAO")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "60"
