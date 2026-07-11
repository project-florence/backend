from fastapi import FastAPI
from src.core.config import init_config
from src.services.bist import cache_tickers_and_companies
from src.api.routes import router

app = FastAPI()

init_config()
cache_tickers_and_companies()


@app.get("/")
def root():
    return {}

@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(router)
