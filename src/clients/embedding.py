import os
from openai import OpenAI
from sklearn.metrics.pairwise import cosine_similarity
from src.core.config import get_config

_client = None
_model = None


def init_client(url=None, model=None, api_key=None):
    global _client, _model
    cfg = get_config().get("embedding", {})
    if url is None:
        url = os.getenv("EMBEDDING_BASE_URL") or cfg.get("base_url")
    if api_key is None:
        api_key = os.getenv("EMBEDDING_API_KEY") or cfg.get("api_key")
    if model is None:
        model = os.getenv("EMBEDDING_MODEL") or cfg.get("model")
    _client = OpenAI(api_key=api_key, base_url=url)
    _model = model


def create_embedding(text: str) -> list[float]:
    global _client, _model
    if _client is None:
        init_client()
    response = _client.embeddings.create(model=_model, input=text)
    return response.data[0].embedding


def similarity(emb1: list[float], emb2: list[float]) -> float:
    return cosine_similarity([emb1], [emb2])[0][0]
