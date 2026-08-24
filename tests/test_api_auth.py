"""Unit tests for src/api/auth.py.

Hermetic: the shared async ``db`` singleton and Redis proxy are swapped for
in-memory fakes (``fake_db``/``fake_redis`` fixtures), and password hashing /
email sending are stubbed so no real Argon2 work, DB, Redis, or mail network
call happens. The router is exercised through httpx ASGITransport against a
minimal FastAPI app.
"""

from datetime import datetime, timedelta, timezone

import pytest
from argon2.exceptions import VerificationError

import src.api.auth as auth_module
from src.api.auth import router as auth_router
from src.clients import mail as mail_module

from api_helpers import FakePasswordHasher, build_app, request

PAST = datetime.now(timezone.utc) - timedelta(hours=1)
FUTURE = datetime.now(timezone.utc) + timedelta(hours=1)


def _patch_ph(monkeypatch):
    fake = FakePasswordHasher()
    monkeypatch.setattr(auth_module, "ph", fake)
    return fake


def _patch_mail(monkeypatch, sent: bool = False):
    async def _no_send_email(*args, **kwargs):
        return sent

    monkeypatch.setattr(mail_module, "send_email", _no_send_email)
    return _no_send_email


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


async def test_register_success(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    _patch_mail(monkeypatch)
    fake_db.queue_fetchone(None, None, (42,))

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/register",
        json={"username": "alice", "email": "Alice@Example.com", "password": "password123"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Register successful"
    assert body["user_id"] == 42
    assert body["verification_sent"] is False
    assert fake_db.commit_calls >= 1
    inserts = [q for q in fake_db.queries if "INSERT INTO users" in q[0]]
    assert len(inserts) == 1
    assert inserts[0][1][1] == "alice@example.com"


async def test_register_duplicate_username(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone((1,))

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/register",
        json={"username": "alice", "email": "alice@example.com", "password": "password123"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "error_username_taken"


async def test_register_duplicate_email(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone(None, (1,))

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/register",
        json={"username": "alice", "email": "alice@example.com", "password": "password123"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "error_email_taken"


async def test_register_bad_payloads(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    app = build_app(auth_router)

    too_short = await request(
        app,
        "POST",
        "/auth/register",
        json={"username": "a", "email": "a@example.com", "password": "short"},
    )
    assert too_short.status_code == 422

    bad_email = await request(
        app,
        "POST",
        "/auth/register",
        json={"username": "a", "email": "not-an-email", "password": "password123"},
    )
    assert bad_email.status_code == 422


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


async def test_login_success(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone((7, "hash:password123", "user", True))

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/login",
        data={"username": "alice", "password": "password123"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert resp.cookies.get("access_token")


async def test_login_wrong_password(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone((7, "hash:password123", "user", True))

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/login",
        data={"username": "alice", "password": "wrongpassword"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "error_login_failed"


async def test_login_unknown_user(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/login",
        data={"username": "ghost", "password": "password123"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "error_login_failed"


async def test_login_unverified_email(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone((7, "hash:password123", "user", False))

    app = build_app(auth_router)
    resp = await request(
        app,
        "POST",
        "/auth/login",
        data={"username": "alice", "password": "password123"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "error_email_not_verified"


# ---------------------------------------------------------------------------
# verify-email
# ---------------------------------------------------------------------------


async def test_verify_email_success(fake_db, fake_redis):
    fake_db.queue_fetchone((7, FUTURE))

    app = build_app(auth_router)
    resp = await request(app, "GET", "/auth/verify-email?token=goodtoken")

    assert resp.status_code == 200
    assert resp.json() == {"message": "Email verified", "email_verified": True}
    update = [q for q in fake_db.queries if "email_verified = TRUE" in q[0]]
    assert len(update) == 1


async def test_verify_email_invalid_token(fake_db, fake_redis):
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(app, "GET", "/auth/verify-email?token=badtoken")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired verification token"


async def test_verify_email_expired_token(fake_db, fake_redis):
    fake_db.queue_fetchone((7, PAST))

    app = build_app(auth_router)
    resp = await request(app, "GET", "/auth/verify-email?token=oldtoken")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired verification token"


# ---------------------------------------------------------------------------
# resend-verification
# ---------------------------------------------------------------------------


async def test_resend_verification_success(monkeypatch, fake_db, fake_redis):
    _patch_mail(monkeypatch, sent=True)
    fake_db.queue_fetchone((7, "alice@example.com", False))

    app = build_app(auth_router)
    resp = await request(
        app, "POST", "/auth/resend-verification", json={"username_or_email": "alice"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"verification_sent": True}


async def test_resend_verification_user_not_found(monkeypatch, fake_db, fake_redis):
    _patch_mail(monkeypatch)
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(
        app, "POST", "/auth/resend-verification", json={"username_or_email": "ghost"}
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found"


async def test_resend_verification_already_verified(monkeypatch, fake_db, fake_redis):
    _patch_mail(monkeypatch)
    fake_db.queue_fetchone((7, "alice@example.com", True))

    app = build_app(auth_router)
    resp = await request(
        app, "POST", "/auth/resend-verification", json={"username_or_email": "alice"}
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Email already verified"


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


async def test_refresh_success_with_body(monkeypatch, fake_db, fake_redis):
    fake_db.queue_fetchone((7,), ("user", True))

    app = build_app(auth_router)
    resp = await request(app, "POST", "/auth/refresh", json={"refresh_token": "tok123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert resp.cookies.get("refresh_token")


async def test_refresh_success_with_cookie(monkeypatch, fake_db, fake_redis):
    fake_db.queue_fetchone((7,), ("user", True))

    app = build_app(auth_router)
    resp = await request(app, "POST", "/auth/refresh", cookies={"refresh_token": "tok123"})

    assert resp.status_code == 200
    assert resp.json()["access_token"]


async def test_refresh_invalid_token(fake_db, fake_redis):
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(app, "POST", "/auth/refresh", json={"refresh_token": "bogus"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or expired refresh token"


async def test_refresh_no_token_no_cookie(fake_db, fake_redis):
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(app, "POST", "/auth/refresh")

    assert resp.status_code == 401


async def test_refresh_unverified_user(fake_db, fake_redis):
    fake_db.queue_fetchone((7,), ("user", False))

    app = build_app(auth_router)
    resp = await request(app, "POST", "/auth/refresh", json={"refresh_token": "tok123"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "error_email_not_verified"


# ---------------------------------------------------------------------------
# logout / delete / account management
# ---------------------------------------------------------------------------


async def test_logout(monkeypatch, fake_db, fake_redis):
    fake_db.rowcount = 1

    app = build_app(auth_router)
    resp = await request(app, "POST", "/auth/logout", json={"refresh_token": "tok123"})

    assert resp.status_code == 200
    assert resp.json() == {"message": "Logged out"}


async def test_logout_without_body_succeeds(fake_db, fake_redis):
    # Body-less logout is what the web client sends via cookies.
    fake_db.rowcount = 1

    app = build_app(auth_router)
    resp = await request(app, "POST", "/auth/logout")

    assert resp.status_code == 200
    assert resp.status_code != 422


async def test_logout_deletes_auth_cookies(fake_db, fake_redis):
    fake_db.rowcount = 1

    app = build_app(auth_router)
    resp = await request(app, "POST", "/auth/logout")

    cookies = resp.headers.get_list("set-cookie")
    by_name = {}
    for c in cookies:
        by_name[c.split("=", 1)[0]] = c

    assert "access_token" in by_name
    assert "refresh_token" in by_name
    assert "path=/api/v1/auth" in by_name["refresh_token"].lower()
    for c in cookies:
        low = c.lower()
        assert "max-age=0" in low or "expires=" in low
        assert "httponly" in low
        assert "samesite=strict" in low


async def test_delete_account(fake_db, fake_redis):
    app = build_app(auth_router)
    resp = await request(app, "DELETE", "/auth/delete")

    assert resp.status_code == 200
    assert resp.json() == {"message": "Deleted user 7"}
    deletes = [q for q in fake_db.queries if "DELETE FROM users" in q[0]]
    assert len(deletes) == 1
    assert fake_db.commit_calls == 1


async def test_change_password_success(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone(("hash:oldpass",))

    app = build_app(auth_router)
    resp = await request(
        app,
        "PUT",
        "/auth/change-password",
        json={"current_password": "oldpass", "new_password": "newpass12345"},
    )

    assert resp.status_code == 200
    assert resp.json()["message"] == "Password changed successfully"
    update = [q for q in fake_db.queries if "UPDATE users SET hashed_pw" in q[0]]
    assert len(update) == 1


async def test_change_password_wrong_current(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone(("hash:oldpass",))

    app = build_app(auth_router)
    resp = await request(
        app,
        "PUT",
        "/auth/change-password",
        json={"current_password": "wrong", "new_password": "newpass12345"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Current password is incorrect"


async def test_change_password_user_not_found(fake_db, fake_redis):
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(
        app,
        "PUT",
        "/auth/change-password",
        json={"current_password": "oldpass", "new_password": "newpass12345"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found"


async def test_change_email_success(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone(("hash:oldpass",), None)

    app = build_app(auth_router)
    resp = await request(
        app,
        "PUT",
        "/auth/change-email",
        json={"new_email": "new@example.com", "current_password": "oldpass"},
    )

    assert resp.status_code == 200
    assert resp.json()["new_email"] == "new@example.com"


async def test_change_email_taken(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone(("hash:oldpass",), (5,))

    app = build_app(auth_router)
    resp = await request(
        app,
        "PUT",
        "/auth/change-email",
        json={"new_email": "taken@example.com", "current_password": "oldpass"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Email already in use"


async def test_change_username_success(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone(("hash:oldpass",), None)

    app = build_app(auth_router)
    resp = await request(
        app,
        "PUT",
        "/auth/change-username",
        json={"new_username": "bob", "current_password": "oldpass"},
    )

    assert resp.status_code == 200
    assert resp.json()["new_username"] == "bob"


async def test_change_username_taken(monkeypatch, fake_db, fake_redis):
    _patch_ph(monkeypatch)
    fake_db.queue_fetchone(("hash:oldpass",), (5,))

    app = build_app(auth_router)
    resp = await request(
        app,
        "PUT",
        "/auth/change-username",
        json={"new_username": "taken", "current_password": "oldpass"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Username already in use"


# ---------------------------------------------------------------------------
# profile / credits / avatar / preferences
# ---------------------------------------------------------------------------


async def test_get_profile(fake_db, fake_redis):
    created_at = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    fake_db.queue_fetchone(
        ("alice", "alice@example.com", "user", created_at, True, "avatar-1"),
        ("user", None),
        (25.0,),
    )

    app = build_app(auth_router)
    resp = await request(app, "GET", "/profile")

    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "alice"
    assert body["email"] == "alice@example.com"
    assert body["credits"] == 25.0
    assert body["email_verified"] is True


async def test_get_profile_not_found(fake_db, fake_redis):
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(app, "GET", "/profile")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found"


async def test_get_credits(fake_db, fake_redis):
    fake_db.queue_fetchone(("user", None), (30.0,))

    app = build_app(auth_router)
    resp = await request(app, "GET", "/credits")

    assert resp.status_code == 200
    assert resp.json() == {"credits": 30.0}


async def test_update_avatar_success(fake_db, fake_redis):
    app = build_app(auth_router)
    resp = await request(app, "PUT", "/profile/avatar", json={"avatar_id": "avatar-3"})

    assert resp.status_code == 200
    assert resp.json() == {"message": "Avatar updated", "avatar_id": "avatar-3"}


async def test_update_avatar_unknown(fake_db, fake_redis):
    app = build_app(auth_router)
    resp = await request(app, "PUT", "/profile/avatar", json={"avatar_id": "nope"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Unknown avatar_id"


async def test_get_preferences_success(fake_db, fake_redis):
    fake_db.queue_fetchone(({"layout": "compact"},))

    app = build_app(auth_router)
    resp = await request(app, "GET", "/user/preferences")

    assert resp.status_code == 200
    assert resp.json() == {"layout": "compact"}


async def test_get_preferences_not_found(fake_db, fake_redis):
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(app, "GET", "/user/preferences")

    assert resp.status_code == 404


async def test_update_preferences_success(fake_db, fake_redis):
    fake_db.queue_fetchone(({"theme": "dark"},))

    app = build_app(auth_router)
    resp = await request(
        app, "PUT", "/user/preferences", json={"prefs": {"language": "en"}}
    )

    assert resp.status_code == 200
    assert resp.json() == {"theme": "dark", "language": "en"}


async def test_update_preferences_not_found(fake_db, fake_redis):
    fake_db.queue_fetchone(None)

    app = build_app(auth_router)
    resp = await request(app, "PUT", "/user/preferences", json={"prefs": {"a": 1}})

    assert resp.status_code == 404