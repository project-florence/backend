import os
import secrets
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import jwt
from fastapi import Depends, HTTPException, status, Header, Cookie, Request
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
                        # Redis'e string cache'lenenle asagida kullanilan yerel
                        # degisken ayni tipte olmali; aksi halde cache-hit
                        # yolunda calisan `datetime.fromisoformat` cache-miss
                        # yolunda datetime nesnesiyle cagrilip TypeError firlatir.
                        changed_at = changed_at.isoformat()
                        await r.set(cache_key, changed_at, ex=60)
                    else:
                        changed_at = ""
                        await r.set(cache_key, changed_at, ex=60)
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


async def get_current_user_optional(request: Request) -> int | None:
    """Gecerli token varsa user_id, yoksa/gecersizse ``None``.

    B-17: public-first okuma uclarinda hem middleware (anonim IP limiti icin
    kimlik ayrimi) hem de handler bagimliligi olarak kullanilir. Middleware
    zaten cozmusse (``request.state.user_id``) JWT/DB'ye tekrar gidilmez.
    """
    # Middleware public okuma yollarinda kullaniciyi bir kez cozer; handler'in
    # da ayni degeri tekrar cozmesi (2x JWT + DB kontrolu) gereksiz olur.
    # ``state`` her Request'te olmayabilir (test sahteleri) -> defansif.
    state = getattr(request, "state", None)
    if state is not None and getattr(state, "user_id", None) is not None:
        return state.user_id
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


async def _lookup_user_type(user_id: int) -> str:
    """``users.user_type`` degerini doner; hata/eksik satirda ``"user"``."""
    from src.core.database import db

    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
            row = await cur.fetchone()
        if row:
            return row[0] or "user"
    except Exception:
        pass  # DB hatasinda admin boost yok; normal limit uygulanir
    return "user"


async def get_current_user_full(token: str | None = Depends(oauth2_scheme), access_token: str | None = Cookie(default=None)):
    """(user_id, user_type) doner — rate limit'te admin boost'u icin.

    get_current_user ile ayni auth akisi; ek olarak users.user_type
    DB'den okunur (JWT'ye dokunulmaz -> eski token'larla da calisir).
    """
    user_id = await get_current_user(token, access_token)
    return user_id, await _lookup_user_type(user_id)


async def get_current_user_full_optional(request: Request) -> tuple[int | None, str | None]:
    """Anonim erisime acik uclarda (ornek ``/news/{ticker}``) (user_id, user_type).

    Anonimde ``(None, None)``; girisli kullanicida admin boost icin user_type
    cozulur (B-17). Boylece gizli ucun auth zorunlulugu kalkarken girisli
    kullanici icin mevcut per-user limit davranisi korunur.
    """
    user_id = await get_current_user_optional(request)
    if user_id is None:
        return None, None
    return user_id, await _lookup_user_type(user_id)


def verify_admin_token(x_admin_token: str = Header(...)):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured")
    # BEKLEYENLER.md: sabit zamanli karsilastirma kullanilmiyordu (str '!='
    # ilk farkli karakterde erken donerse timing side-channel'a acik) --
    # secrets.compare_digest ile duzeltildi.
    # bytes'a cevriliyor: compare_digest str uzerinde yalniz ASCII kabul
    # eder, ASCII disi bir header degeri TypeError -> 500 uretirdi.
    if not secrets.compare_digest(x_admin_token.encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return True


async def validate_ticker(ticker: str):
    from src.services.bist import is_valid_bist_ticker
    if not await is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail="error_invalid_ticker")


def _legacy_symbol_map() -> dict[str, str]:
    """SYMBOL_REGISTRY legacy adi -> kanonik sembol eslemesi."""
    from src.finance.symbols import SYMBOL_REGISTRY
    return {d.legacy_name: d.canonical for d in SYMBOL_REGISTRY.values() if d.legacy_name}


async def validate_symbol(ticker: str) -> str:
    """BIST ticker VEYA kanonik ekonomi sembolunu dogrular (B-16).

    Favoriler gibi hem hisse hem FX/kiymetli maden kabul eden uclar icin ortak
    dogrulama. Gecerli sembolun kanonik halini doner: BIST buyuk harfe cevrilir;
    ekonomi sembolleri kanonik anahtar (``USD``, ``XAU-GRAM``) ya da legacy ad
    (``gram-altin``) olarak kabul edilip kanonige normalize edilir. Gecersiz
    sembol, mevcut BIST davranisiyla tutarli olarak 404 alir.
    """
    from src.services.bist import is_valid_bist_ticker
    from src.finance.symbols import SYMBOL_REGISTRY

    if not ticker:
        raise HTTPException(status_code=404, detail="error_invalid_ticker")

    stripped = ticker.strip()
    upper = stripped.upper()
    if upper in SYMBOL_REGISTRY:
        return upper

    legacy = _legacy_symbol_map()
    if stripped.lower() in legacy:
        return legacy[stripped.lower()]

    if await is_valid_bist_ticker(upper):
        return upper

    raise HTTPException(status_code=404, detail="error_invalid_ticker")
