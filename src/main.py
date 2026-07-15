from fastapi import FastAPI
from src.core.config import init_config, get_config
from src.core.database import init_db
from src.clients.llm import init_client as init_llm_client
from src.clients.embedding import init_client as init_embedding_client
from src.services.bist import cache_tickers_and_companies
from src.api.router import router

app = FastAPI()

init_config()
init_db()
init_llm_client(
    url=get_config()["article_analyzer"]["llm_url"],
    default_model=get_config()["article_analyzer"]["llm_model"],
)
init_embedding_client()
cache_tickers_and_companies()


@app.get("/")
def root():
    return {}

@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(router)