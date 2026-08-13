"""Bot hesaplari yonetimi.

Kullanici kendi bot hesaplarini acar (owner_id -> current_user), listeler ve
siler. Bot hesaplari ``user_type='bot'`` ile isaretlenir; e-postalari
``<username>@bot.florencex.com.tr`` bicimindedir. Botlar owner'in kredisinden
harcar (src/services/credits.py icindeki owner cozumlemesi).

Sifre yalnizca olusturma yanitinda TEK SEFERLIK doner; bir daha okunamaz.
"""

import asyncio
import secrets
import string

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import get_current_user
from src.core.database import db

ph = PasswordHasher()

router = APIRouter()

MAX_BOTS_PER_USER = 5
BOT_EMAIL_DOMAIN = "bot.florencex.com.tr"


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


class BotCreate(BaseModel):
    username: str = Field(min_length=3, max_length=255)
    password: str | None = Field(default=None, min_length=10)


@router.post("/bots")
async def create_bot(payload: BotCreate, current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT user_type FROM users WHERE id = %s", (current_user_id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        if row[0] == "bot":
            # Bot hesaplari baska bot acamaz.
            raise HTTPException(status_code=403, detail="error_bots_not_allowed")

        await cur.execute(
            "SELECT COUNT(*) FROM users WHERE owner_id = %s AND user_type = 'bot'",
            (current_user_id,),
        )
        if (await cur.fetchone())[0] >= MAX_BOTS_PER_USER:
            raise HTTPException(status_code=400, detail="error_bot_limit_reached")

        # Username benzersizligi (email de ayni username'den turedigi icin
        # benzersizdir; case varyantlarinda email UNIQUE constraint yakalar).
        await cur.execute("SELECT id FROM users WHERE username = %s", (payload.username,))
        if await cur.fetchone() is not None:
            raise HTTPException(status_code=409, detail="error_username_taken")

        password = payload.password or _random_password()
        email = f"{payload.username}@{BOT_EMAIL_DOMAIN}"
        hashed_pw = await asyncio.to_thread(ph.hash, password)

        try:
            await cur.execute(
                """INSERT INTO users (username, email, hashed_pw, user_type, owner_id, email_verified)
                   VALUES (%s, %s, %s, 'bot', %s, TRUE) RETURNING id""",
                (payload.username, email, hashed_pw, current_user_id),
            )
            bot_id = (await cur.fetchone())[0]
            await db.commit()
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=409, detail="error_username_taken")

    # Sifre yalnizca bu yanitta doner (tek seferlik).
    return {
        "id": bot_id,
        "username": payload.username,
        "email": email,
        "password": password,
    }


@router.get("/bots")
async def list_bots(current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT id, username, created_at, last_login FROM users "
            "WHERE owner_id = %s AND user_type = 'bot' ORDER BY created_at",
            (current_user_id,),
        )
        rows = await cur.fetchall()

    return {
        "bots": [
            {
                "id": r[0],
                "username": r[1],
                "created_at": r[2].isoformat() if r[2] else None,
                "last_login": r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ]
    }


@router.delete("/bots/{bot_id}")
async def delete_bot(bot_id: int, current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "DELETE FROM users WHERE id = %s AND owner_id = %s AND user_type = 'bot'",
            (bot_id, current_user_id),
        )
        if cur.rowcount == 0:
            await db.rollback()
            raise HTTPException(status_code=404, detail="Bot not found")
        await db.commit()

    return {"message": f"Bot {bot_id} deleted"}
