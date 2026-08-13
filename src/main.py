import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api.deps import SECRET_KEY, get_current_user_optional
from src.api.router import router
from src.clients.cron import cron_client
from src.clients.embedding import init_client as init_embedding_client
from src.clients.http import close_client
from src.clients.llm import init_client as init_llm_client
from src.core.config import init_config, is_production
from src.core.database import db, init_db
from src.core.logging import init_logging
from src.cron.register import register_cron_jobs
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
    init_llm_client()
    init_embedding_client()
    await cache_tickers_and_companies()

    await cron_client.init()
    await register_cron_jobs()
    await cron_client.start()
    yield
    await cron_client.stop()
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
        "/api/v1/market/status",
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

    path = request.url.path

    # CORS preflight (OPTIONS) istekleri auth gerektirmez.
    if request.method == "OPTIONS":
        return await call_next(request)

    is_public = any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS if p.startswith("/api/"))

    if path.startswith("/api/") and not is_public:
        try:
            user_id = await get_current_user_optional(request)
            if user_id is None:
                return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
            request.state.user_id = user_id
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
