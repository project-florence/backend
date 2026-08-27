"""Unit tests for `src/api/deps.py` -- TEST_COVERAGE_PLAN.md Adim A.

`get_current_user_optional` middleware'in (src/main.py) kullandigi kapi;
`get_current_user`/`get_current_user_full` ise Depends() ile router'lara
enjekte edilen surumler. Hermetik: `fake_db`/`fake_redis`
(tests/conftest.py) DB/Redis singleton'larini in-memory sahtelerle degistirir.
`test_main_middleware.py` bu fonksiyonlari middleware uzerinden dolayli
kapsar; burada dogrudan (Depends zincirinden bagimsiz) davranislari test
ediyoruz.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException

from src.api import deps as deps_module
from src.api.deps import (
    ALGORITHM,
    SECRET_KEY,
    get_current_user,
    get_current_user_full,
    get_current_user_optional,
    verify_admin_token,
)
from src.core import database as db_module
from src.core import redis as redis_module


def _make_token(user_id=7, iat=None, exp=None, secret=None, **extra):
    now = datetime.now(timezone.utc)
    payload = {"user_id": user_id, "iat": int((iat or now).timestamp())}
    if exp is not None:
        payload["exp"] = int(exp.timestamp())
    payload.update(extra)
    return jwt.encode(payload, secret or SECRET_KEY, algorithm=ALGORITHM)


def _fake_request(bearer=None, cookie=None):
    """`get_current_user_optional` sadece `.headers.get(...)` ve
    `.cookies.get(...)` kullanir -- tam bir Starlette Request'e gerek yok."""
    headers = {}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    cookies = {}
    if cookie is not None:
        cookies["access_token"] = cookie
    return SimpleNamespace(headers=headers, cookies=cookies)


# ---------------------------------------------------------------------------
# get_current_user_optional
# ---------------------------------------------------------------------------


async def test_optional_bearer_valid(fake_db, fake_redis):
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=11)

    user_id = await get_current_user_optional(_fake_request(bearer=token))

    assert user_id == 11


async def test_optional_cookie_valid(fake_db, fake_redis):
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=12)

    user_id = await get_current_user_optional(_fake_request(cookie=token))

    assert user_id == 12


async def test_optional_bearer_takes_priority_over_cookie(fake_db, fake_redis):
    """Ikisi de gelirse Authorization header once kontrol edilir."""
    fake_db.queue_fetchone((None,), (False,))
    bearer_token = _make_token(user_id=1)
    cookie_token = _make_token(user_id=2)

    user_id = await get_current_user_optional(
        _fake_request(bearer=bearer_token, cookie=cookie_token)
    )

    assert user_id == 1


async def test_optional_no_token_returns_none(fake_db, fake_redis):
    user_id = await get_current_user_optional(_fake_request())

    assert user_id is None
    assert fake_db.queries == []


async def test_optional_malformed_bearer_returns_none(fake_db, fake_redis):
    user_id = await get_current_user_optional(_fake_request(bearer="garbage"))

    assert user_id is None


async def test_optional_non_bearer_authorization_falls_back_to_cookie(fake_db, fake_redis):
    """'Bearer ' ile baslamayan Authorization header'i yok sayilir, cookie'ye
    bakilir."""
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=5)

    request = _fake_request(cookie=token)
    request.headers["Authorization"] = "Basic something"

    user_id = await get_current_user_optional(request)

    assert user_id == 5


async def test_optional_expired_token_returns_none(fake_db, fake_redis):
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    token = _make_token(iat=past - timedelta(seconds=1), exp=past)

    user_id = await get_current_user_optional(_fake_request(bearer=token))

    assert user_id is None


async def test_optional_wrong_secret_returns_none(fake_db, fake_redis):
    token = _make_token(secret="a-completely-different-secret-value")

    user_id = await get_current_user_optional(_fake_request(bearer=token))

    assert user_id is None


async def test_optional_token_without_user_id_claim_returns_none(fake_db, fake_redis):
    """'user_id' claim'i olmayan (baska amacli veya bozuk) bir JWT reddedilir."""
    token = jwt.encode(
        {"iat": int(datetime.now(timezone.utc).timestamp())}, SECRET_KEY, algorithm=ALGORITHM
    )

    user_id = await get_current_user_optional(_fake_request(bearer=token))

    assert user_id is None


async def test_optional_token_without_iat_claim_skips_password_check(fake_db, fake_redis):
    """'iat' claim'i olmayan bir token icin sifre-degisikligi kontrolu
    tamamen atlanir, dogrudan is_frozen kontrolune gecilir."""
    token = jwt.encode({"user_id": 7}, SECRET_KEY, algorithm=ALGORITHM)
    fake_db.queue_fetchone((False,))  # sadece is_frozen sorgusu

    user_id = await get_current_user_optional(_fake_request(bearer=token))

    assert user_id == 7
    assert not any("password_changed_at" in q[0] for q in fake_db.queries)


async def test_optional_user_deleted_after_token_issued_returns_none(fake_db, fake_redis):
    """Token gecerli imzalansa da kullanici artik DB'de yoksa (silinmis)
    reddedilir."""
    token = _make_token(user_id=999)
    fake_db.queue_fetchone(None)  # password_changed_at sorgusu: satir yok

    user_id = await get_current_user_optional(_fake_request(bearer=token))

    assert user_id is None


async def test_password_changed_naive_datetime_treated_as_utc_and_rejects(fake_db, fake_redis):
    """DB'den tzinfo'suz (naive) bir datetime donerse UTC varsayilir --
    yine de token'dan sonraki degisiklik dogru reddedilir."""
    iat = datetime.now(timezone.utc) - timedelta(minutes=10)
    changed_naive = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None)
    token = _make_token(user_id=7, iat=iat)
    fake_db.queue_fetchone((changed_naive,))

    user_id = await get_current_user_optional(_fake_request(bearer=token))

    assert user_id is None


async def test_password_changed_before_token_iat_allows_through(fake_db, fake_redis):
    """Sifre token'dan ONCE degismisse (token hala gecerli araligi
    kapsiyor) reddedilmemeli."""
    changed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    iat = datetime.now(timezone.utc) - timedelta(minutes=1)
    token = _make_token(user_id=7, iat=iat)
    fake_db.queue_fetchone((changed_at,), (False,))  # password_changed_at, is_frozen

    user_id = await get_current_user_optional(_fake_request(bearer=token))

    assert user_id == 7


async def test_is_frozen_redis_set_failure_is_swallowed(fake_db, fake_redis, monkeypatch):
    """Frozen durumu Redis'e cache'lenirken (r.set) hata olursa sessizce
    yutulur -- dogru sonuc yine de donmeli (Redis down = cache'siz mod)."""
    fake_db.queue_fetchone((True,))  # is_frozen

    async def _boom_set(*args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_module.r, "set", _boom_set)

    frozen = await deps_module._is_frozen(7)

    assert frozen is True


# ---------------------------------------------------------------------------
# get_current_user (Depends(oauth2_scheme) + Cookie param) -- dogrudan cagri
# ---------------------------------------------------------------------------


async def test_get_current_user_valid_bearer(fake_db, fake_redis):
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=42)

    user_id = await get_current_user(token=token, access_token=None)

    assert user_id == 42


async def test_get_current_user_valid_cookie_fallback(fake_db, fake_redis):
    fake_db.queue_fetchone((None,), (False,))
    token = _make_token(user_id=43)

    user_id = await get_current_user(token=None, access_token=token)

    assert user_id == 43


async def test_get_current_user_bearer_priority_over_cookie(fake_db, fake_redis):
    fake_db.queue_fetchone((None,), (False,))
    bearer_token = _make_token(user_id=1)
    cookie_token = _make_token(user_id=2)

    user_id = await get_current_user(token=bearer_token, access_token=cookie_token)

    assert user_id == 1


async def test_get_current_user_no_token_raises_401(fake_db, fake_redis):
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(token=None, access_token=None)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token"


async def test_get_current_user_invalid_token_raises_401(fake_db, fake_redis):
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(token="not-a-jwt", access_token=None)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token"


async def test_get_current_user_expired_token_raises_401(fake_db, fake_redis):
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    token = _make_token(iat=past - timedelta(seconds=1), exp=past)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(token=token, access_token=None)

    assert exc_info.value.status_code == 401


async def test_get_current_user_frozen_user_raises_401(fake_db, fake_redis):
    fake_db.queue_fetchone((None,), (True,))  # password_changed_at, is_frozen=True
    token = _make_token(user_id=7)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(token=token, access_token=None)

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# get_current_user_full -- (user_id, user_type)
# ---------------------------------------------------------------------------


async def test_get_current_user_full_returns_user_type(fake_db, fake_redis):
    fake_db.queue_fetchone((None,), (False,), ("admin",))
    token = _make_token(user_id=7)

    user_id, user_type = await get_current_user_full(token=token, access_token=None)

    assert user_id == 7
    assert user_type == "admin"


async def test_get_current_user_full_defaults_to_user_on_none_type(fake_db, fake_redis):
    fake_db.queue_fetchone((None,), (False,), (None,))
    token = _make_token(user_id=7)

    _, user_type = await get_current_user_full(token=token, access_token=None)

    assert user_type == "user"


async def test_get_current_user_full_defaults_to_user_when_row_missing(fake_db, fake_redis):
    """users.user_type sorgusu None donerse (satir bulunamadi) admin boost
    yok sayilir, normal limit uygulanir."""
    fake_db.queue_fetchone((None,), (False,), None)
    token = _make_token(user_id=7)

    _, user_type = await get_current_user_full(token=token, access_token=None)

    assert user_type == "user"


async def test_get_current_user_full_swallows_db_error_defaults_to_user(fake_db, fake_redis, monkeypatch):
    """user_type sorgusu patlarsa (DB hatasi) admin boost'u sessizce
    atlanir -- get_current_user_full 500 firlatmaz, dogrudan "user" doner."""
    fake_db.queue_fetchone((None,), (False,))  # _decode_user zincirini besler
    token = _make_token(user_id=7)

    original_cursor = fake_db.cursor
    call_count = {"n": 0}

    def _cursor_third_call_boom(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:  # 1: password_changed_at, 2: is_frozen, 3: user_type
            raise RuntimeError("db patladi")
        return original_cursor(*args, **kwargs)

    monkeypatch.setattr(db_module.db, "cursor", _cursor_third_call_boom)

    user_id, user_type = await get_current_user_full(token=token, access_token=None)

    assert user_id == 7
    assert user_type == "user"


async def test_get_current_user_full_propagates_auth_failure(fake_db, fake_redis):
    """Auth basarisiz olursa (get_current_user 401 firlatirsa) user_type
    sorgusuna hic gidilmeden HTTPException yukari tasar."""
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_full(token=None, access_token=None)

    assert exc_info.value.status_code == 401
    assert fake_db.queries == []


# ---------------------------------------------------------------------------
# verify_admin_token
# ---------------------------------------------------------------------------


def test_verify_admin_token_correct_token(monkeypatch):
    monkeypatch.setattr(deps_module, "ADMIN_TOKEN", "secret-admin-token")

    assert verify_admin_token(x_admin_token="secret-admin-token") is True


def test_verify_admin_token_wrong_token(monkeypatch):
    monkeypatch.setattr(deps_module, "ADMIN_TOKEN", "secret-admin-token")

    with pytest.raises(HTTPException) as exc_info:
        verify_admin_token(x_admin_token="wrong-token")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid admin token"


def test_verify_admin_token_not_configured(monkeypatch):
    """ADMIN_TOKEN ortam degiskeni tanimsizken (None/bos) 500 doner --
    yanlislikla her x-admin-token degerini kabul etmek yerine kapaniyor."""
    monkeypatch.setattr(deps_module, "ADMIN_TOKEN", None)

    with pytest.raises(HTTPException) as exc_info:
        verify_admin_token(x_admin_token="anything")

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "ADMIN_TOKEN not configured"


def test_verify_admin_token_empty_string_not_configured(monkeypatch):
    monkeypatch.setattr(deps_module, "ADMIN_TOKEN", "")

    with pytest.raises(HTTPException) as exc_info:
        verify_admin_token(x_admin_token="")

    assert exc_info.value.status_code == 500
