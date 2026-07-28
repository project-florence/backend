import os
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
from src.api.deps import SECRET_KEY

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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/")
def root():
    return {}

@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(router)