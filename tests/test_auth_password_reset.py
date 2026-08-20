"""Unit tests for forgot-password / reset-password in src/api/auth.py.

Hermetic: the shared async ``db`` singleton and Redis proxy are swapped for
in-memory fakes (``fake_db``/``fake_redis`` fixtures), and password hashing /
email sending are stubbed so no real Argon2 work, DB, Redis, or mail network
call happens. The router is exercised through httpx ASGITransport against a
minimal FastAPI app.
"""

import hashlib
import re

import src.api.auth as auth_module
from src.api.auth import router as auth_router
from src.clients import mail as mail_module

from api_helpers import FakePasswordHasher, build_app, request

PASSWORD_RESET_TTL_MINUTES = 30


def _patch_ph(monkeypatch):
    fake = FakePasswordHasher()
    monkeypatch.setattr(auth_module, "ph", fake)
    return fake


def _capture_mail(monkeypatch):
    calls = []

    async def _send_email(to, subject, html, **kwargs):
        calls.append({"to": to, "subject": subject, "html": html})
        return True

    monkeypatch.setattr(mail_module, "send_email", _send_email)
    return calls


def _extract_token_from_url(html: str) -> str:
    m = re.search(r"/reset-password\?token=([A-Za-z0-9_-]+)", html)
    assert m, f"no reset token in html: {html[:200]}"
    return m.group(1)


def _stored_token_hash(fake_db) -> str | None:
    for query, params in fake_db.queries:
        if "INSERT INTO password_resets" in query:
            return params[1]
    return None


# ---------------------------------------------------------------------------
# forgot-password
# ---------------------------------------------------------------------------


async def test_forgot_password_existing_user(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    calls = _capture_mail(monkeypatch)
    fake_db.queue_fetchone((42, "alice@example.com"))

    app = build_app(auth_router)
    resp = await request(
        app, "POST", "/auth/forgot-password", json={"email": "Alice@Example.com"}
    )

    assert resp.status_code == 200
    assert resp.json()["message"] == "if the account exists, a password reset link was sent"

    # Email case-normalized lookup + a single insert into password_resets.
    lookups = [q for q in fake_db.queries if "FROM users" in q[0] and "lower(email)" in q[0]]
    assert lookups and lookups[0][1][0] == "alice@example.com"
    inserts = [q for q in fake_db.queries if "INSERT INTO password_resets" in q[0]]
    assert len(inserts) == 1
    assert fake_db.commit_calls >= 1

    # Mail was sent with the reset url; the token in the url, when hashed,
    # must equal the token_hash stored in the DB.
    assert len(calls) == 1
    assert calls[0]["to"] == "alice@example.com"
    assert calls[0]["subject"] == "Florence — Şifreni Sıfırla"
    token = _extract_token_from_url(calls[0]["html"])
    assert "/reset-password?token=" in calls[0]["html"]
    stored_hash = _stored_token_hash(fake_db)
    assert stored_hash is not None
    assert stored_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert stored_hash != token


async def test_forgot_password_stores_expiry(monkeypatch, fake_db, fake_redis):
    calls = _capture_mail(monkeypatch)
    fake_db.queue_fetchone((7, "bob@example.com"))

    app = build_app(auth_router)
    resp = await request(
        app, "POST", "/auth/forgot-password", json={"email": "bob@example.com"}
    )
    assert resp.status_code == 200

    inserts = [q for q in fake_db.queries if "INSERT INTO password_resets" in q[0]]
    assert len(inserts) == 1
    user_id, token_hash, expires_at = inserts[0][1]
    assert user_id == 7
    assert token_hash
    # expires_at should be ~ ttl minutes in the future.
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    delta = (expires_at - now).total_seconds()
    assert 0 < delta <= PASSWORD_RESET_TTL_MINUTES * 60


async def test_forgot_password_unknown_email(monkeypatch, fake_db, fake_redis):
    calls = _capture_mail(monkeypatch)
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(
        app, "POST", "/auth/forgot-password", json={"email": "ghost@example.com"}
    )

    assert resp.status_code == 200
    assert calls == []
    inserts = [q for q in fake_db.queries if "INSERT INTO password_resets" in q[0]]
    assert inserts == []


async def test_forgot_password_rate_limited(monkeypatch, fake_db, fake_redis):
    _capture_mail(monkeypatch)
    fake_db.queue_fetchone((1, "a@example.com"))

    app = build_app(auth_router)
    for _ in range(6):
        resp = await request(
            app, "POST", "/auth/forgot-password", json={"email": "a@example.com"}
        )
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# reset-password
# ---------------------------------------------------------------------------


async def test_reset_password_valid_token(monkeypatch, fake_db, fake_redis):
    fake = _patch_ph(monkeypatch)
    fake_db.queue_fetchone((99, 42))

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/reset-password",
        json={"token": "goodtoken", "new_password": "newpass12345"},
    )

    assert resp.status_code == 200
    assert resp.json()["message"] == "password reset successful"
    user_update = [q for q in fake_db.queries if "UPDATE users SET hashed_pw" in q[0]]
    assert len(user_update) == 1
    # hashed password stored for user 42 with the fake hasher.
    assert user_update[0][1][0] == "hash:newpass12345"
    assert user_update[0][1][1] == 42
    # used_at set on the reset row.
    used_updates = [q for q in fake_db.queries if "UPDATE password_resets SET used_at" in q[0]]
    assert len(used_updates) == 1
    assert used_updates[0][1][0] == 99
    assert fake_db.commit_calls >= 1


async def test_reset_password_invalid_token(fake_db, fake_redis):
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/reset-password",
        json={"token": "badtoken", "new_password": "newpass12345"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "error_invalid_or_expired_token"


async def test_reset_password_weak_password(fake_db, fake_redis):
    fake_db.queue_fetchone((99, 42))

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/reset-password",
        json={"token": "goodtoken", "new_password": "short"},
    )

    assert resp.status_code == 422


async def test_reset_password_no_commit_on_invalid(fake_db, fake_redis):
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/reset-password",
        json={"token": "badtoken", "new_password": "newpass12345"},
    )

    assert resp.status_code == 400
    updates = [q for q in fake_db.queries if "UPDATE users SET hashed_pw" in q[0]]
    assert updates == []
