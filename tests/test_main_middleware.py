"""Unit tests for `src/main.py::auth_and_tracking_middleware` -- TEST_COVERAGE_PLAN.md Adim A.

Bu middleware `/api/*` isteklerinin gectigi tek kapi; burada bir hata auth
bypass demek. `@app.middleware("http")` dekoratoru fonksiyonu degistirmeden
dondurur (Starlette ``add_middleware(BaseHTTPMiddleware, dispatch=func)``),
bu yuzden ``main_module.auth_and_tracking_middleware`` dogrudan cagrilabilir
-- tam ASGI/TestClient kurulumuna (ve dolayisiyla lifespan/DB/Redis'e) hic
gerek yok. ``request``/``call_next`` elle insa edilir (Starlette ``Request``
+ kucuk bir spy), ``fake_db``/``fake_redis`` (tests/conftest.py) DB/Redis'i
hermetik tutar.

Kapsanan: PUBLIC_PATHS allowlist'i (dogru path'ler gecer, bilinen korumali
path'ler yanlislikla sete eklenmemis -- guvenlik yuzeyi testi), JWT
gecerli/suresi dolmus/bozuk/yanlis imza, cookie ve bearer kollari,
`password_changed_at` iptali + 60s Redis cache (hit/miss), `is_frozen`
iptali + 30s Redis cache (hit/miss), `PoolTimeout` -> 503 (401 DEGIL),
`db.release_current()` finally cagrisi, analytics tracking'in
`/api/v1/analytics/event` icin atlanmasi.
"""

import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from psycopg_pool import PoolTimeout
from starlette.requests import Request
from starlette.responses import Response

import src.main as main_module
from src.api.deps import ALGORITHM, SECRET_KEY

middleware = main_module.auth_and_tracking_middleware


def _make_request(path, method="GET", token=None, cookie_token=None):
    """Middleware'in kullandigi minimum ASGI scope'undan bir Request insa eder."""
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if cookie_token is not None:
        headers.append((b"cookie", f"access_token={cookie_token}".encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "query_string": b"",
        "client": ("testclient", 1234),
    }
    return Request(scope)


def _make_token(user_id=7, iat=None, exp=None, secret=None, **extra):
    now = datetime.now(timezone.utc)
    payload = {"user_id": user_id, "iat": int((iat or now).timestamp())}
    if exp is not None:
        payload["exp"] = int(exp.timestamp())
    payload.update(extra)
    return jwt.encode(payload, secret or SECRET_KEY, algorithm=ALGORITHM)


class _CallNextSpy:
    """call_next stub'i: cagrildi mi izler, sabit bir Response doner (ya da hata firlatir)."""

    def __init__(self, response=None, exc=None):
        self.called = False
        self.received_request = None
        self._response = response or Response("ok", status_code=200)
        self._exc = exc

    async def __call__(self, request):
        self.called = True
        self.received_request = request
        if self._exc is not None:
            raise self._exc
        return self._response


@pytest.fixture
def fire_calls(monkeypatch):
    """`fire_and_forget` gercekte `asyncio.create_task` ile arka plan islemi
    baslatir; testin event loop'u kapanana kadar askida kalmamasi icin
    cagrilari kaydeden senkron bir sahte ile degistiriyoruz (urun kodu
    davranisini degistirmiyor -- sadece test-zamanli monkeypatch)."""
    calls = []

    def _fake(event_type, user_id=None, ticker=None, details=None):
        calls.append({"event_type": event_type, "user_id": user_id, "details": details})

    monkeypatch.setattr(main_module, "fire_and_forget", _fake)
    return calls


def _detail(response):
    return json.loads(response.body)["detail"]


# ---------------------------------------------------------------------------
# PUBLIC_PATHS allowlist
# ---------------------------------------------------------------------------


async def test_public_path_passes_without_token(fake_db, fake_redis, fire_calls):
    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/market/status"), call_next)

    assert response.status_code == 200
    assert call_next.called is True
    assert fire_calls == []  # public path analytics tracking'e girmez


async def test_public_path_prefix_match(fake_db, fake_redis, fire_calls):
    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/legal/kvkk"), call_next)

    assert response.status_code == 200
    assert call_next.called is True


async def test_public_prefix_does_not_match_partial_word(fake_db, fake_redis, fire_calls):
    """'/api/v1/legal' oneki '/' sinirinda eslesmeli; '/api/v1/legalxyz'
    yanlislikla public sayilmamali."""
    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/legalxyz"), call_next)

    assert response.status_code == 401
    assert call_next.called is False


async def test_non_api_path_bypasses_auth_entirely(fake_db, fake_redis, fire_calls):
    call_next = _CallNextSpy()
    response = await middleware(_make_request("/health"), call_next)

    assert response.status_code == 200
    assert call_next.called is True


async def test_options_preflight_bypasses_auth(fake_db, fake_redis, fire_calls):
    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", method="OPTIONS"), call_next)

    assert response.status_code == 200
    assert call_next.called is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/portfolio",
        "/api/v1/auth/change-password",
        "/api/v1/auth/delete",
        "/api/v1/reports",
        "/api/v1/simulation",
        "/api/v1/data/export",
        "/api/v1/bots",
        "/api/v1/favorites",
        "/api/v1/profile",
    ],
)
async def test_known_sensitive_paths_are_not_public(path, fake_db, fake_redis, fire_calls):
    """Guvenlik yuzeyi testi: bir endpoint'i public yapmak = PUBLIC_PATHS
    setine eklemek demek. Bu path'ler token'siz istekte 401 almali; biri
    yanlislikla sete eklenirse bu test kirilir."""
    call_next = _CallNextSpy()
    response = await middleware(_make_request(path), call_next)

    assert response.status_code == 401
    assert _detail(response) == "Not authenticated"
    assert call_next.called is False


async def test_missing_token_401(fake_db, fake_redis, fire_calls):
    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio"), call_next)

    assert response.status_code == 401
    assert _detail(response) == "Not authenticated"
    assert call_next.called is False


# ---------------------------------------------------------------------------
# JWT: gecerli / suresi dolmus / bozuk / yanlis imza
# ---------------------------------------------------------------------------


async def test_valid_bearer_token_passes(fake_db, fake_redis, fire_calls):
    fake_db.queue_fetchone((None,), (False,))  # password_changed_at, is_frozen
    token = _make_token(user_id=7)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 200
    assert call_next.called is True
    assert call_next.received_request.state.user_id == 7
    assert len(fire_calls) == 1
    assert fire_calls[0]["user_id"] == 7


async def test_valid_cookie_token_passes(fake_db, fake_redis, fire_calls):
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=9)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", cookie_token=token), call_next)

    assert response.status_code == 200
    assert call_next.received_request.state.user_id == 9


async def test_expired_token_401(fake_db, fake_redis, fire_calls):
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    token = _make_token(user_id=7, iat=past - timedelta(seconds=1), exp=past)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 401
    assert call_next.called is False
    assert fake_db.queries == []  # decode basarisiz -> DB'ye hic gidilmez


async def test_malformed_token_401(fake_db, fake_redis, fire_calls):
    call_next = _CallNextSpy()
    response = await middleware(
        _make_request("/api/v1/portfolio", token="not-a-jwt-at-all"), call_next
    )

    assert response.status_code == 401
    assert call_next.called is False


async def test_wrong_secret_signature_401(fake_db, fake_redis, fire_calls):
    token = _make_token(user_id=7, secret="a-completely-different-secret-value")

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 401
    assert call_next.called is False


# ---------------------------------------------------------------------------
# Iptal kontrolu 1: password_changed_at > token.iat (60s Redis cache)
# ---------------------------------------------------------------------------


async def test_password_changed_after_token_rejected_cache_miss(fake_db, fake_redis, fire_calls):
    iat = datetime.now(timezone.utc) - timedelta(minutes=10)
    changed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    token = _make_token(user_id=7, iat=iat)
    fake_db.queue_fetchone((changed_at,))  # password_changed_at sorgusu

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 401
    assert call_next.called is False
    assert fake_redis.store["user:pwd_changed:7"] == changed_at.isoformat()  # 60s cache'lendi


async def test_password_changed_cache_hit_rejects_without_db(fake_db, fake_redis, fire_calls):
    iat = datetime.now(timezone.utc) - timedelta(minutes=10)
    changed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    fake_redis.store["user:pwd_changed:7"] = changed_at.isoformat()
    token = _make_token(user_id=7, iat=iat)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 401
    assert fake_db.queries == []  # cache hit -> DB'ye hic gidilmedi


async def test_password_unchanged_cache_hit_empty_sentinel_passes(fake_db, fake_redis, fire_calls):
    """DB'de password_changed_at NULL ise '' sentinel'i 60s cache'lenir;
    sonraki istek password_changed_at icin DB'ye gitmeden gecer."""
    iat = datetime.now(timezone.utc) - timedelta(minutes=10)
    fake_redis.store["user:pwd_changed:7"] = ""
    fake_db.queue_fetchone((False,))  # sadece is_frozen sorgusu kalir
    token = _make_token(user_id=7, iat=iat)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 200
    assert not any("password_changed_at" in q[0] for q in fake_db.queries)


# ---------------------------------------------------------------------------
# Iptal kontrolu 2: is_frozen (30s Redis cache)
# ---------------------------------------------------------------------------


async def test_is_frozen_rejects_cache_miss(fake_db, fake_redis, fire_calls):
    fake_redis.store["user:pwd_changed:7"] = ""  # sifre kontrolu DB'ye gitmesin
    fake_db.queue_fetchone((True,))  # is_frozen
    token = _make_token(user_id=7)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 401
    assert fake_redis.store["user:frozen:7"] == "1"  # 30s cache'lendi


async def test_is_frozen_rejects_cache_hit_without_db(fake_db, fake_redis, fire_calls):
    fake_redis.store["user:pwd_changed:7"] = ""
    fake_redis.store["user:frozen:7"] = "1"
    token = _make_token(user_id=7)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 401
    assert fake_db.queries == []


async def test_not_frozen_cache_hit_passes(fake_db, fake_redis, fire_calls):
    fake_redis.store["user:pwd_changed:7"] = ""
    fake_redis.store["user:frozen:7"] = "0"
    token = _make_token(user_id=7)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 200
    assert fake_db.queries == []


# ---------------------------------------------------------------------------
# PoolTimeout -> 503 (401 DEGIL) -- bilincli tasarim: frontend refresh
# zincirine girmesin
# ---------------------------------------------------------------------------


async def test_pool_timeout_returns_503_not_401(monkeypatch, fake_db, fake_redis, fire_calls):
    async def _raise_pool_timeout(request):
        raise PoolTimeout("pool exhausted")

    monkeypatch.setattr(main_module, "get_current_user_optional", _raise_pool_timeout)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token="irrelevant"), call_next)

    assert response.status_code == 503
    assert response.status_code != 401
    assert _detail(response) == "Database busy, please retry"
    assert call_next.called is False


async def test_unexpected_exception_in_auth_returns_401(monkeypatch, fake_db, fake_redis, fire_calls):
    """PoolTimeout disinda kalan diger istisnalar (orn. beklenmedik decode
    hatasi) 401'e dusuyor -- yalnizca PoolTimeout 503 ozel muamelesi gorur."""

    async def _raise_value_error(request):
        raise ValueError("boom")

    monkeypatch.setattr(main_module, "get_current_user_optional", _raise_value_error)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token="irrelevant"), call_next)

    assert response.status_code == 401
    assert _detail(response) == "Not authenticated"


# ---------------------------------------------------------------------------
# Baglanti iadesi: db.release_current() finally'de cagriliyor
# ---------------------------------------------------------------------------


async def test_release_current_called_after_protected_request(fake_db, fake_redis, fire_calls):
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=7)

    call_next = _CallNextSpy()
    await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert fake_db.release_calls == 1


async def test_release_current_called_after_public_request(fake_db, fake_redis, fire_calls):
    call_next = _CallNextSpy()
    await middleware(_make_request("/api/v1/market/status"), call_next)

    assert fake_db.release_calls == 1


async def test_release_current_called_even_when_call_next_raises(fake_db, fake_redis, fire_calls):
    call_next = _CallNextSpy(exc=RuntimeError("downstream patladi"))

    with pytest.raises(RuntimeError):
        await middleware(_make_request("/api/v1/market/status"), call_next)

    assert fake_db.release_calls == 1


async def test_release_current_not_reached_on_401_short_circuit(fake_db, fake_redis, fire_calls):
    """401 erken donusu call_next'i sarmalayan try/finally'e hic girmez --
    bu istekte baglanti hic alinmadigi icin release_current cagirilmaz
    (sizinti degil, mevcut tasarimin bir sonucu; davranisi belgeliyoruz)."""
    call_next = _CallNextSpy()
    await middleware(_make_request("/api/v1/portfolio"), call_next)

    assert fake_db.release_calls == 0


# ---------------------------------------------------------------------------
# Analytics tracking: /api/v1/analytics/event istisnasi
# ---------------------------------------------------------------------------


async def test_analytics_event_path_excluded_from_tracking(fake_db, fake_redis, fire_calls):
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=7)

    call_next = _CallNextSpy()
    response = await middleware(
        _make_request("/api/v1/analytics/event", token=token), call_next
    )

    assert response.status_code == 200
    assert fire_calls == []


async def test_other_protected_path_is_tracked(fake_db, fake_redis, fire_calls):
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=7)

    call_next = _CallNextSpy()
    response = await middleware(_make_request("/api/v1/portfolio", token=token), call_next)

    assert response.status_code == 200
    assert len(fire_calls) == 1
    assert fire_calls[0]["event_type"] == "api_request"
    assert fire_calls[0]["details"]["endpoint"] == "/api/v1/portfolio"
    assert fire_calls[0]["details"]["status_code"] == 200


# ---------------------------------------------------------------------------
# global_exception_handler -- route icinde firlatilan PoolTimeout de 503,
# diger her sey 500 (detay sizdirmadan)
# ---------------------------------------------------------------------------


async def test_global_exception_handler_pool_timeout_returns_503():
    response = await main_module.global_exception_handler(
        _make_request("/api/v1/portfolio"), PoolTimeout("pool exhausted")
    )

    assert response.status_code == 503
    assert _detail(response) == "Database busy, please retry"


async def test_global_exception_handler_generic_exception_returns_500():
    response = await main_module.global_exception_handler(
        _make_request("/api/v1/portfolio"), RuntimeError("beklenmedik hata")
    )

    assert response.status_code == 500
    assert _detail(response) == "Internal server error"
    # Istisna metni (dahili detay) yanita sizmamali.
    assert "beklenmedik hata" not in response.body.decode()


# ---------------------------------------------------------------------------
# Trivial public handlers (root/health) -- /health PUBLIC_PATHS'te, saglik
# kontrolu icin auth gerektirmemesi kritik
# ---------------------------------------------------------------------------


async def test_root_handler():
    assert await main_module.root() == {}


async def test_health_handler():
    assert await main_module.health() == {"status": "ok"}
