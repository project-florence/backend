import asyncio
import datetime
import json
import logging
import os
import secrets

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field

from src.api.deps import SECRET_KEY, ALGORITHM, get_current_user
from src.core.config import get_config
from src.core.database import db
from src.core.ratelimit import rate_limiter
from src.services.credits import get_total as get_credits
from src.services.credits import init_user_credits
from src.services.refresh_token import (
    create_refresh_token,
    hash_token,
    rotate_token,
    revoke_all_user_tokens,
    revoke_token,
    refresh_token_ttl_days,
)

logger = logging.getLogger(__name__)

ph = PasswordHasher()
router = APIRouter()


def _set_auth_cookies(response, access_token: str, refresh_token: str):
    secure = os.getenv("ENVIRONMENT", "development") == "production"
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=3600,
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=refresh_token_ttl_days() * 24 * 3600,
        path="/api/v1/auth",
    )


def _delete_auth_cookies(response):
    secure = os.getenv("ENVIRONMENT", "development") == "production"
    response.delete_cookie(key="access_token", httponly=True, secure=secure, samesite="strict", path="/")
    response.delete_cookie(key="refresh_token", httponly=True, secure=secure, samesite="strict", path="/api/v1/auth")


class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str = Field(min_length=10)


class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10)


class UpdateEmail(BaseModel):
    new_email: EmailStr
    current_password: str


class UpdateUsername(BaseModel):
    new_username: str
    current_password: str


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class ResendVerification(BaseModel):
    # Kullanici adi VEYA e-posta (login ile ayni esnekligi sunar).
    username_or_email: str


class ForgotPassword(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str
    new_password: str = Field(min_length=10)


def create_jwt_token(user_id: int):
    payload = {
        "user_id": user_id,
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/auth/register")
async def auth_register(request: Request, user: UserRegister):
    client_ip = request.client.host if request.client else "unknown"
    await rate_limiter.check(f"register:{client_ip}", max_requests=3, window_seconds=60)

    email = user.email.lower()
    async with db.cursor(row_factory=None) as cur:
        # Ayri ayri kontrol: frontend i18n icin spesifik hata kodlari.
        await cur.execute("SELECT id FROM users WHERE username = %s", (user.username,))
        if await cur.fetchone() is not None:
            raise HTTPException(status_code=400, detail="error_username_taken")
        await cur.execute("SELECT id FROM users WHERE lower(email) = %s", (email,))
        if await cur.fetchone() is not None:
            raise HTTPException(status_code=400, detail="error_email_taken")

        hashed_pw = await asyncio.to_thread(ph.hash, user.password)

        verify_token = secrets.token_urlsafe(32)
        verify_expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)

        try:
            await cur.execute(
                """INSERT INTO users (username, email, hashed_pw, email_verify_token, email_verify_expires_at)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (user.username, email, hashed_pw, verify_token, verify_expires)
            )
            new_user_id = (await cur.fetchone())[0]
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

    # Yeni kullaniciya baslangic kredisi (DEFAULT_CREDITS). Hata kayit
    # akisini kirmaz; gunluk refill cron'u telafi eder.
    try:
        await init_user_credits(new_user_id)
    except Exception as e:
        logger.warning("Initial credits could not be granted to user %s: %s", new_user_id, e)

    # Dogrulama maili: hata asla kayit akisini kirmaz (sessiz devam).
    verification_sent = False
    try:
        from src.clients.mail import render_template, send_email

        base_url = os.getenv("PUBLIC_BASE_URL") or str(request.base_url)
        base_url = base_url.rstrip("/")
        verify_url = f"{base_url}/verify-email?token={verify_token}"
        html = render_template("verify_email.html", verify_url=verify_url)
        verification_sent = await send_email(
            email, "Florence — E-postanı Doğrula", html
        )
    except Exception as e:
        logger.warning("Verification email could not be sent to %s: %s", email, e)

    return {
        "message": "Register successful",
        "user_id": new_user_id,
        "verification_sent": verification_sent,
    }


@router.post("/auth/login")
async def auth_login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    await rate_limiter.check(f"login:{form_data.username}", max_requests=5, window_seconds=60)

    async with db.cursor(row_factory=None) as cur:
        # Kullanici adi VEYA e-posta ile giris (e-posta kucuk harfe normalize).
        await cur.execute(
            "SELECT id, hashed_pw, user_type, email_verified FROM users WHERE username = %s OR lower(email) = %s",
            (form_data.username, form_data.username.lower()),
        )
        user_row = await cur.fetchone()

        if not user_row:
            raise HTTPException(status_code=400, detail="error_login_failed")

        user_id, db_password_hash, user_type, email_verified = user_row
        try:
            await asyncio.to_thread(ph.verify, db_password_hash, form_data.password)
        except VerificationError:
            raise HTTPException(status_code=400, detail="error_login_failed")

        # E-posta dogrulanmamis hesaplar giris yapamaz (botlar haric).
        # SQL'e kosul KOYULMAZ: hesap varligi sizintisini onlemek icin kontrol
        # sifre dogrulamasindan SONRA kodda yapilir.
        if not email_verified and user_type != "bot":
            raise HTTPException(status_code=403, detail="error_email_not_verified")

        await cur.execute("UPDATE users SET last_login = NOW() WHERE id = %s", (user_id,))

        access_token = create_jwt_token(user_id)
        refresh_token = await create_refresh_token(user_id, device=request.client.host if request.client else None)
        response = JSONResponse(content={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        })
        _set_auth_cookies(response, access_token, refresh_token)
        return response


@router.get("/auth/verify-email")
async def verify_email(token: str = Query(..., description="E-posta dogrulama token'i")):
    """E-posta dogrulama (public). Token eslesir + sure gecmemisse dogrulanmis isaretlenir."""
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT id, email_verify_expires_at FROM users WHERE email_verify_token = %s",
            (token,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Invalid or expired verification token")

        user_id, expires_at = row
        if expires_at is None or expires_at < datetime.datetime.now(datetime.timezone.utc):
            raise HTTPException(status_code=400, detail="Invalid or expired verification token")

        await cur.execute(
            "UPDATE users SET email_verified = TRUE, email_verify_token = NULL, email_verify_expires_at = NULL WHERE id = %s",
            (user_id,),
        )
        await db.commit()

    return {"message": "Email verified", "email_verified": True}


@router.post("/auth/resend-verification")
async def resend_verification(request: Request, payload: ResendVerification):
    """Dogrulama mailini yeniden gonderir (public, rate limited).

    Kullanici adi VEYA e-posta alir. Hesap dogrulanmissa 400; degilse yeni
    token (24 saat) uretilir, DB'ye yazilir ve mail gonderilir. Mail hatalari
    asla 5xx uretmez: ``verification_sent: false`` ile 200 doner.
    """
    account = payload.username_or_email.strip()
    await rate_limiter.check(f"resend-verif:{account}", max_requests=3, window_seconds=3600)

    async with db.cursor(row_factory=None) as cur:
        # Login ile ayni eslesme: username tam, e-posta kucuk harfe normalize.
        await cur.execute(
            "SELECT id, email, email_verified FROM users "
            "WHERE username = %s OR lower(email) = %s",
            (account, account.lower()),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        user_id, email, email_verified = row
        if email_verified:
            raise HTTPException(status_code=400, detail="Email already verified")

        verify_token = secrets.token_urlsafe(32)
        verify_expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
        try:
            await cur.execute(
                "UPDATE users SET email_verify_token = %s, email_verify_expires_at = %s "
                "WHERE id = %s",
                (verify_token, verify_expires, user_id),
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning("resend-verification: token yazilamadi (user %s): %s", user_id, e)
            raise HTTPException(status_code=500, detail="Database error")

    # Mail: hata asla kullaniciyi kitlemesin — sessiz devam (register deseni).
    verification_sent = False
    try:
        from src.clients.mail import render_template, send_email

        base_url = os.getenv("PUBLIC_BASE_URL") or str(request.base_url)
        base_url = base_url.rstrip("/")
        verify_url = f"{base_url}/verify-email?token={verify_token}"
        html = render_template("verify_email.html", verify_url=verify_url)
        verification_sent = await send_email(
            email, "Florence — E-postanı Doğrula", html
        )
    except Exception as e:
        logger.warning("Resend verification email could not be sent to %s: %s", email, e)

    return {"verification_sent": verification_sent}


@router.post("/auth/refresh")
async def auth_refresh(request: Request, payload: RefreshRequest | None = None):
    # Body yoksa (veya token body'de verilmediyse) cookie'den oku. Null body
    # 422 uretmez; frontend cookie akisi boylece refresh edebilir.
    token = (payload.refresh_token if payload else None) or request.cookies.get("refresh_token")
    await rate_limiter.check(f"refresh:{hash_token(token) if token else 'none'}", max_requests=5, window_seconds=60)

    result = await rotate_token(token)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    new_refresh_token, user_id = result

    # E-posta dogrulama kontrolu (login ile ayni): dogrulanmamis hesaplar
    # refresh ile token alamaz, botlar istisnadir. Kontrol kodda — hesap
    # varligi sizintisi olmasin diye SQL'e kosul eklenmez.
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT user_type, email_verified FROM users WHERE id = %s", (user_id,))
        user_row = await cur.fetchone()
    if user_row is None or (not user_row[1] and user_row[0] != "bot"):
        raise HTTPException(status_code=403, detail="error_email_not_verified")

    access_token = create_jwt_token(user_id)
    response = JSONResponse(content={
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
    })
    _set_auth_cookies(response, access_token, new_refresh_token)
    return response


@router.post("/auth/logout")
async def auth_logout(request: Request, payload: RefreshRequest | None = None):
    # Body yoksa (veya token body'de verilmediyse) cookie'den oku. Null body
    # 422 uretmez; frontend cookie akisi boylece logout edebilir.
    token = (payload.refresh_token if payload else None) or request.cookies.get("refresh_token")
    if token:
        await revoke_token(token)
    response = JSONResponse(content={"message": "Logged out"})
    _delete_auth_cookies(response)
    return response


@router.delete("/auth/delete")
async def auth_delete(current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute("DELETE FROM users WHERE id = %s", (current_user_id,))
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=400, detail="Database error")
    return {"message": f"Deleted user {current_user_id}"}


@router.put("/auth/change-password")
async def change_password(payload: ChangePassword, current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT hashed_pw FROM users WHERE id = %s", (current_user_id,))
        user_row = await cur.fetchone()

        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")

        db_password_hash = user_row[0]

        try:
            await asyncio.to_thread(ph.verify, db_password_hash, payload.current_password)
        except VerificationError:
            raise HTTPException(status_code=400, detail="Current password is incorrect")

        new_hashed_pw = await asyncio.to_thread(ph.hash, payload.new_password)
        try:
            await cur.execute("UPDATE users SET hashed_pw = %s, password_changed_at = NOW() WHERE id = %s", (new_hashed_pw, current_user_id))
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

        await revoke_all_user_tokens(current_user_id)

    return {"message": "Password changed successfully"}


@router.post("/auth/forgot-password")
async def forgot_password(request: Request, payload: ForgotPassword):
    """Sifre sifirlama baglantisini e-posta ile gonderir (public, rate limited).

    Hesap var olup olmamasindan bagimsiz olarak her zaman 200 doner: hesap
    varligi sizintisini onlemek icin. Kullanici varsa hashed token uretilir,
    ``password_resets`` tablosuna yazilir ve sifirlama maili gonderilir.
    Mail hatalari asla 5xx uretmez (register deseni).
    """
    email = payload.email.lower()
    client_ip = request.client.host if request.client else "unknown"
    await rate_limiter.check(
        f"forgot-password:{email or client_ip}", max_requests=5, window_seconds=3600
    )

    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT id, email FROM users WHERE lower(email) = %s", (email,))
        row = await cur.fetchone()

        if row:
            user_id = row[0]
            token = secrets.token_urlsafe(32)
            token_hash = hash_token(token)
            ttl_minutes = int(get_config()["auth"]["password_reset_ttl_minutes"])
            expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=ttl_minutes)
            try:
                await cur.execute(
                    "INSERT INTO password_resets (user_id, token_hash, expires_at) "
                    "VALUES (%s, %s, %s)",
                    (user_id, token_hash, expires_at),
                )
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.warning("forgot-password: token yazilamadi (user %s): %s", user_id, e)
                return {"message": "if the account exists, a password reset link was sent"}

            try:
                from src.clients.mail import render_template, send_email

                base_url = os.getenv("PUBLIC_BASE_URL") or str(request.base_url)
                base_url = base_url.rstrip("/")
                reset_url = f"{base_url}/reset-password?token={token}"
                html = render_template("reset_password.html", reset_url=reset_url)
                await send_email(email, "Florence — Şifreni Sıfırla", html)
            except Exception as e:
                logger.warning("Reset password email could not be sent to %s: %s", email, e)

    return {"message": "if the account exists, a password reset link was sent"}


@router.post("/auth/reset-password")
async def reset_password(payload: ResetPassword):
    """Sifirlama token'i ile sifreyi degistirir (public)."""
    token_hash = hash_token(payload.token)

    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT id, user_id FROM password_resets "
            "WHERE token_hash = %s AND used_at IS NULL AND expires_at > NOW()",
            (token_hash,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="error_invalid_or_expired_token")

        reset_id, user_id = row
        new_hashed_pw = await asyncio.to_thread(ph.hash, payload.new_password)
        try:
            await cur.execute(
                "UPDATE users SET hashed_pw = %s, password_changed_at = NOW() WHERE id = %s",
                (new_hashed_pw, user_id),
            )
            await cur.execute(
                "UPDATE password_resets SET used_at = NOW() WHERE id = %s", (reset_id,)
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning("reset-password: guncelleme basarisiz (reset %s): %s", reset_id, e)
            raise HTTPException(status_code=500, detail="Database error")

    await revoke_all_user_tokens(user_id)

    return {"message": "password reset successful"}


@router.put("/auth/change-email")
async def change_email(payload: UpdateEmail, current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT hashed_pw FROM users WHERE id = %s", (current_user_id,))
        user_row = await cur.fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")

        try:
            await asyncio.to_thread(ph.verify, user_row[0], payload.current_password)
        except VerificationError:
            raise HTTPException(status_code=400, detail="Current password is incorrect")

        await cur.execute("SELECT id FROM users WHERE email = %s AND id != %s", (payload.new_email, current_user_id))
        if await cur.fetchone():
            raise HTTPException(status_code=400, detail="Email already in use")

        try:
            await cur.execute("UPDATE users SET email = %s WHERE id = %s", (payload.new_email, current_user_id))
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

        await revoke_all_user_tokens(current_user_id)

    return {"message": "Email changed successfully", "new_email": payload.new_email}


@router.put("/auth/change-username")
async def change_username(payload: UpdateUsername, current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT hashed_pw FROM users WHERE id = %s", (current_user_id,))
        user_row = await cur.fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")

        try:
            await asyncio.to_thread(ph.verify, user_row[0], payload.current_password)
        except VerificationError:
            raise HTTPException(status_code=400, detail="Current password is incorrect")

        await cur.execute("SELECT id FROM users WHERE username = %s AND id != %s", (payload.new_username, current_user_id))
        if await cur.fetchone():
            raise HTTPException(status_code=400, detail="Username already in use")

        try:
            await cur.execute("UPDATE users SET username = %s WHERE id = %s", (payload.new_username, current_user_id))
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

        await revoke_all_user_tokens(current_user_id)

    return {"message": "Username changed successfully", "new_username": payload.new_username}


@router.get("/profile")
async def get_profile(current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute("""
                SELECT username, email, user_type, created_at, email_verified, avatar_id
                FROM users WHERE id = %s
            """, (current_user_id,))
            row = await cur.fetchone()

        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "username": row[0],
        "email": row[1],
        "user_type": row[2],
        "created_at": row[3].isoformat() if row[3] else None,
        "email_verified": row[4],
        "avatar_id": row[5],
        "credits": await get_credits(current_user_id)
    }


class AvatarUpdate(BaseModel):
    avatar_id: str


@router.put("/profile/avatar")
async def update_avatar(payload: AvatarUpdate, current_user_id: int = Depends(get_current_user)):
    from src.api.meta import AVATAR_IDS

    if payload.avatar_id not in AVATAR_IDS:
        raise HTTPException(status_code=400, detail="Unknown avatar_id")

    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute(
                "UPDATE users SET avatar_id = %s WHERE id = %s",
                (payload.avatar_id, current_user_id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

    return {"message": "Avatar updated", "avatar_id": payload.avatar_id}


@router.get("/credits")
async def get_credits_endpoint(current_user_id: int = Depends(get_current_user)):
    return {"credits": await get_credits(current_user_id)}


class PreferencesUpdate(BaseModel):
    prefs: dict


@router.get("/user/preferences")
async def get_preferences(current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT prefs FROM user_preferences WHERE user_id = %s", (current_user_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Preferences not found")
    return row[0]


@router.put("/user/preferences")
async def update_preferences(payload: PreferencesUpdate, current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT prefs FROM user_preferences WHERE user_id = %s", (current_user_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Preferences not found")

        existing = row[0]
        existing.update(payload.prefs)

        await cur.execute(
            "UPDATE user_preferences SET prefs = %s, updated_at = NOW() WHERE user_id = %s",
            (json.dumps(existing), current_user_id)
        )
        await db.commit()
    return existing
