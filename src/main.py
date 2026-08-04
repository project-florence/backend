import os
import time
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from src.core.logging import init_logging
from src.core.config import init_config, is_production
from src.core.database import init_db
from src.clients.llm import init_client as init_llm_client
from src.clients.embedding import init_client as init_embedding_client
from src.services.bist import cache_tickers_and_companies
from src.api.router import router
from src.api.deps import SECRET_KEY, get_current_user_optional
from src.services.analytics import track_event

logger = logging.getLogger(__name__)

init_logging()

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is required")

docs_enabled = not is_production()
app = FastAPI(docs_url="/docs" if docs_enabled else None,
              redoc_url="/redoc" if docs_enabled else None,
              openapi_url="/openapi.json" if docs_enabled else None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if is_production() else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_config()
init_db()
init_llm_client()
init_embedding_client()
cache_tickers_and_companies()


@app.middleware("http")
async def auth_and_tracking_middleware(request: Request, call_next):
    PUBLIC_PATHS = {
        "/api/v1/auth/login",
        "/api/v1/auth/register",
        "/api/v1/auth/logout",
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
    is_public = any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS if p.startswith("/api/"))

    if path.startswith("/api/") and not is_public:
        try:
            user_id = get_current_user_optional(request)
            if user_id is None:
                return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
            request.state.user_id = user_id
        except Exception:
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})

    start = time.perf_counter()
    response = await call_next(request)
    duration = int((time.perf_counter() - start) * 1000)

    if path.startswith("/api/") and not is_public and path != "/api/v1/analytics/event":
        user_id = getattr(request.state, "user_id", None)
        track_event("api_request", user_id=user_id, details={
            "method": request.method,
            "endpoint": path,
            "status_code": response.status_code,
            "response_time_ms": duration,
        })

    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/")
def root():
    return {}

@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(router)
