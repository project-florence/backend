from fastapi import FastAPI
from src.core.config import init_config
from src.core.database import init_db
from src.services.bist import cache_tickers_and_companies
from src.api.router import router

app = FastAPI()

init_config()
init_db()
cache_tickers_and_companies()


@app.get("/")
def root():
    return {}

@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(router)
