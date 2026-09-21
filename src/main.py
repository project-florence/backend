import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import PoolTimeout

from src.api.deps import SECRET_KEY, get_current_user_optional
from src.api.router import router
from src.clients.cron import cron_client
from src.clients.http import close_client
from src.core.config import init_config, is_production
from src.core.database import db, init_db
from src.core.logging import init_logging
from src.core.ratelimit import client_ip, rate_limiter
from src.cron.register import register_cron_jobs
from src.finance import finance_service
from src.services.analytics import aclose as analytics_aclose
from src.services.analytics import fire_and_forget
from src.services.bist import cache_tickers_and_companies

logger = logging.getLogger(__name__)

init_logging()

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is required")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startuplar: config, DB, external client'lar ve ticker cache'i.
    init_config()
    await init_db()
    # LLM istemcileri artik istek/ajan basina TEK llm_settings satirindan
    # cozuluyor (REFACTOR_PLAN.md Adim 2, Adim 6.5) -- burada eager init YOK.
    await cache_tickers_and_companies()

    await cron_client.init()
    await register_cron_jobs()
    await cron_client.start()
    # Finance warm-up (design spec 3.5 / Faz 2-3): one quotes refresh so the
    # first request does not pay source latency. Must be failure-tolerant —
    # a missing DB/Redis or dead sources must NOT prevent app startup.
    try:
        await finance_service.warm_startup()
    except Exception:
        logger.exception("finance warm_startup failed; continuing startup")
    yield
    await cron_client.stop()
    await analytics_aclose()
    await db.close()
    await close_client()


docs_enabled = not is_production()
app = FastAPI(docs_url="/docs" if docs_enabled else None,
              redoc_url="/redoc" if docs_enabled else None,
              openapi_url="/openapi.json" if docs_enabled else None,
              lifespan=lifespan)

# Built-in avatar görselleri (backend/avatars/*.svg) — tüm istemciler (web/desktop/mobile)
# buradan alır; ayrı kopyalama gerekmez.
app.mount("/avatars", StaticFiles(directory="avatars"), name="avatars")

DESKTOP_ORIGINS = [
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=DESKTOP_ORIGINS if is_production() else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_and_tracking_middleware(request: Request, call_next):
    PUBLIC_PATHS = {
        "/api/v1/auth/login",
        "/api/v1/auth/register",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
        "/api/v1/auth/verify-email",
        "/api/v1/auth/resend-verification",
        "/api/v1/auth/forgot-password",
        "/api/v1/auth/reset-password",
        "/api/v1/market/status",
        "/api/v1/data/export/download",
        "/api/v1/meta/avatars",
        "/api/v1/legal",
        "/api/v1/about",
        "/api/v1/contact",
        "/api/v1/version",
        "/api/v1/maintenance",
        "/api/v1/contributors",
        "/",
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    }

    # B-17 — public-first piyasa okumasi: bu yollar YALNIZ okuma metotlari
    # (GET/HEAD) icin public'tir; ayni path'e POST/PUT/DELETE gelirse auth
    # aranir. Deger, giris yapmamis (anonim) istekler icin IP basina
    # dakikalik istek limitidir. Prefix eslesmesi ("p" veya "p/...") ile
    # kapsanan uclar tam olarak okuma uclaridir; kisisel uclar (favorites,
    # portfolios, reports, ...) bu listede DEGILDIR.
    PUBLIC_READ_PATHS: dict[str, int] = {
        "/api/v1/companies/summary": 60,
        "/api/v1/companies/info": 60,
        "/api/v1/companies/search": 60,
        "/api/v1/price/current": 60,
        "/api/v1/price/history": 60,
        "/api/v1/economy/quotes": 60,
        "/api/v1/economy/history": 60,
        "/api/v1/ipos": 60,
        "/api/v1/news": 10,
        "/api/v1/digest": 60,
    }

    path = request.url.path

    # CORS preflight (OPTIONS) istekleri auth gerektirmez.
    if request.method == "OPTIONS":
        return await call_next(request)

    def _is_always_public() -> bool:
        # METHOD'dan bagimsiz public yollar (login/register/legal/...). Prefix
        # eslesmesi "/" sinirinda yapilir: "/api/v1/legalxyz" public SAYILMAZ.
        return any(
            path == p or path.startswith(p + "/")
            for p in PUBLIC_PATHS
            if p.startswith("/api/")
        )

    always_public = _is_always_public()

    read_scope: str | None = None
    read_limit: int | None = None
    if request.method in ("GET", "HEAD"):
        for prefix, limit in PUBLIC_READ_PATHS.items():
            if path == prefix or path.startswith(prefix + "/"):
                read_scope, read_limit = prefix, limit
                break

    is_public = always_public or read_limit is not None

    if path.startswith("/api/") and read_limit is not None and not always_public:
        # Anonim IP limiti. Girisli kullaniciyi anonime karismamak icin once
        # opsiyonel auth cozulur: token yoksa/gecersizse kullanici None kalir
        # -> IP kovasina yazilir. Token'i gecerli olan istek IP limitine
        # girmez (authenticated uclarda per-user limit zaten var).
        user_id: int | None = None
        try:
            user_id = await get_current_user_optional(request)
        except PoolTimeout:
            return JSONResponse(status_code=503, content={"detail": "Database busy, please retry"})
        except Exception:
            user_id = None
        if user_id is not None:
            request.state.user_id = user_id
        else:
            try:
                await rate_limiter.check(
                    f"anon:{read_scope}:{client_ip(request)}",
                    max_requests=read_limit,
                    window_seconds=60,
                )
            except HTTPException as exc:
                if exc.status_code == 429:
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "error_rate_limited"},
                        headers=exc.headers or {"Retry-After": "60"},
                    )
                raise

    if path.startswith("/api/") and not is_public:
        try:
            user_id = await get_current_user_optional(request)
            if user_id is None:
                return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
            request.state.user_id = user_id
        except PoolTimeout:
            # Havuz tukenmis: kullaniciyi 401 ile login'e atma, 503 ver ki
            # frontend refresh/logout zincirine girmesin.
            return JSONResponse(status_code=503, content={"detail": "Database busy, please retry"})
        except Exception:
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})

    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        # Her istekten sonra task baglantisini havuza iade et (sizinti onleme).
        await db.release_current()

    duration = int((time.perf_counter() - start) * 1000)

    if path.startswith("/api/") and not is_public and path != "/api/v1/analytics/event":
        user_id = getattr(request.state, "user_id", None)
        fire_and_forget("api_request", user_id=user_id, details={
            "method": request.method,
            "endpoint": path,
            "status_code": response.status_code,
            "response_time_ms": duration,
        })

    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Baglanti havuzu tukenirse (cron sizintisi / asiri yuk) kullaniciya anlamli
    # bir 503 ver; 30sn bekleyip 500 donmek yerine hizli cevap ver.
    if exc.__class__.__name__ == "PoolTimeout":
        logger.error("DB pool exhausted on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=503, content={"detail": "Database busy, please retry"})
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/")
async def root():
    return {}


@app.get("/health")
async def health():
    return {"status": "ok"}


app.include_router(router)
