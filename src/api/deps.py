import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import jwt
from fastapi import Depends, HTTPException, status, Header, Cookie
from fastapi.security import OAuth2PasswordBearer

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


async def _is_frozen(user_id: int) -> bool:
    """Kullanici dondurulmus mu? (Redis 30s cache; down ise her sefer DB)."""
    from src.core.database import db
    from src.core.redis import r

    cache_key = f"user:frozen:{user_id}"
    cached = await r.get(cache_key)
    if cached is not None:
        return cached == "1"

    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT is_frozen FROM users WHERE id = %s", (user_id,))
        row = await cur.fetchone()
    frozen = bool(row and row[0])

    try:
        await r.set(cache_key, "1" if frozen else "0", ex=30)
    except Exception:
        pass
    return frozen


async def _decode_user(jwt_token: str) -> int | None:
    try:
        payload = jwt.decode(jwt_token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        if user_id is None:
            return None
        token_iat = payload.get("iat")
        if token_iat is not None:
            # Token claim'lerine dokunmadan (format degismesin) sifre degisiklik
            # zamanini Redis'te 60s TTL ile cache'le; miss'te DB'den oku.
            # Redis down ise r.get None doner -> dogrudan DB'ye dusulur.
            from src.core.database import db
            from src.core.redis import r

            cache_key = f"user:pwd_changed:{user_id}"
            changed_at = await r.get(cache_key)
            if changed_at is None:
                async with db.cursor(row_factory=None) as cur:
                    await cur.execute("SELECT password_changed_at FROM users WHERE id = %s", (user_id,))
                    row = await cur.fetchone()
                    if row is None:
                        return None
                    changed_at = row[0]
                    if changed_at is not None:
                        await r.set(cache_key, changed_at.isoformat(), ex=60)
                    else:
                        await r.set(cache_key, "", ex=60)
            if changed_at not in (None, ""):
                changed_dt = datetime.fromisoformat(changed_at)
                if changed_dt.tzinfo is None:
                    changed_dt = changed_dt.replace(tzinfo=timezone.utc)
                if token_iat < changed_dt.timestamp():
                    return None
        # Dondurulmus (frozen) kullanici: token gecerli olsa bile erisim yok.
        if await _is_frozen(user_id):
            return None
        return user_id
    except jwt.PyJWTError:
        return None


async def get_current_user_optional(request):
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return await _decode_user(auth[7:])
    cookies = request.cookies
    token = cookies.get("access_token")
    if token:
        return await _decode_user(token)
    return None


async def get_current_user(token: str | None = Depends(oauth2_scheme), access_token: str | None = Cookie(default=None)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    jwt_token = token or access_token
    if jwt_token is None:
        raise credentials_exception
    user_id = await _decode_user(jwt_token)
    if user_id is None:
        raise credentials_exception
    return user_id


def verify_admin_token(x_admin_token: str = Header(...)):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return True


async def validate_ticker(ticker: str):
    from src.services.bist import is_valid_bist_ticker
    if not await is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
