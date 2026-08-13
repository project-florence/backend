import asyncio
import datetime
import json
import os

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field

from src.api.deps import SECRET_KEY, ALGORITHM, get_current_user
from src.core.database import db
from src.core.ratelimit import rate_limiter
from src.services.credits import get_total as get_credits
from src.services.refresh_token import (
    create_refresh_token,
    hash_token,
    rotate_token,
    revoke_all_user_tokens,
    revoke_token,
    refresh_token_ttl_days,
)

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
    response.delete_cookie(key="access_token", path="/")
    response.delete_cookie(key="refresh_token", path="/api/v1/auth")


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

    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (user.username, user.email))
        if await cur.fetchone() is not None:
            raise HTTPException(status_code=400, detail="Registration failed")

        hashed_pw = await asyncio.to_thread(ph.hash, user.password)

        try:
            await cur.execute(
                "INSERT INTO users (username, email, hashed_pw) VALUES (%s, %s, %s) RETURNING id",
                (user.username, user.email, hashed_pw)
            )
            new_user_id = (await cur.fetchone())[0]
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

    return {"message": "Register successful", "user_id": new_user_id}


@router.post("/auth/login")
async def auth_login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    await rate_limiter.check(f"login:{form_data.username}", max_requests=5, window_seconds=60)

    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT id, hashed_pw FROM users WHERE username = %s", (form_data.username,))
        user_row = await cur.fetchone()

        if not user_row:
            raise HTTPException(status_code=400, detail="Incorrect username or password")

        user_id, db_password_hash = user_row
        try:
            await asyncio.to_thread(ph.verify, db_password_hash, form_data.password)
        except VerificationError:
            raise HTTPException(status_code=400, detail="Incorrect username or password")

        access_token = create_jwt_token(user_id)
        refresh_token = await create_refresh_token(user_id, device=request.client.host if request.client else None)
        response = JSONResponse(content={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        })
        _set_auth_cookies(response, access_token, refresh_token)
        return response


@router.post("/auth/refresh")
async def auth_refresh(request: Request, payload: RefreshRequest):
    token = payload.refresh_token or request.cookies.get("refresh_token")
    await rate_limiter.check(f"refresh:{hash_token(token) if token else 'none'}", max_requests=5, window_seconds=60)

    result = await rotate_token(token)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    new_refresh_token, user_id = result
    access_token = create_jwt_token(user_id)
    response = JSONResponse(content={
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
    })
    _set_auth_cookies(response, access_token, new_refresh_token)
    return response


@router.post("/auth/logout")
async def auth_logout(request: Request, payload: RefreshRequest):
    token = payload.refresh_token or request.cookies.get("refresh_token")
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
                SELECT username, email, user_type, created_at FROM users WHERE id = %s
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
        "credits": await get_credits(current_user_id)
    }


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
