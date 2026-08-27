"""Semantik gomme (embedding) istemcisi.

Yapilandirma kaynagi ``llm_settings`` (``purpose="embedding"``) --
``EMBEDDING_*`` env okumalari YOK (REFACTOR_PLAN.md Adim 2). Digest/rapordan
farkli olarak gomme bir pydantic-ai ajani DEGIL, dogrudan OpenAI-uyumlu
``embeddings`` ucnoktasina giden ince bir cagri -- bu yuzden kendi cagri
yolunu korur, sadece yapilandirmayi ``src.llm.settings.resolve_purpose``'tan
alir.

Her cagirida yeniden cozulur: ayri bir "init" adimi yok, kalici bir istemci
singleton'i tutulmuyor. ``resolve_purpose``'un kendi Redis onbellegi (<=60s
TTL, bkz. ``src/llm/settings.py``) sayesinde bu pratikte neredeyse ucretsiz;
``AsyncOpenAI`` nesnesi kurmak da ucuz (httpx sarici, baglanti havuzu yeniden
acilmiyor). Kazanc: bir ayar degisikligi restart beklemeden bir dakika
icinde etkili olur.
"""

import logging

from openai import AsyncOpenAI
from sklearn.metrics.pairwise import cosine_similarity

from src.llm.settings import LLMPurposeUnconfigured, ResolvedLLM, resolve_purpose

logger = logging.getLogger(__name__)

# src.llm.settings.LLMPurposeUnconfigured ile AYNI tip -- bu modulun kendi
# alaninda daha okunakli bir isimle tekrar disa aktariliyor, boylece
# cagiranlar "embedding yapilandirilmamis" durumunu bu isimle yakalayabilir.
EmbeddingUnconfigured = LLMPurposeUnconfigured


async def create_embedding(text: str) -> list[float]:
    resolved = await resolve_purpose("embedding")
    if not isinstance(resolved, ResolvedLLM):
        raise EmbeddingUnconfigured(resolved)

    client = AsyncOpenAI(api_key=resolved.api_key or "not-needed", base_url=resolved.base_url)
    response = await client.embeddings.create(model=resolved.model, input=text)
    return response.data[0].embedding


def similarity(emb1: list[float], emb2: list[float]) -> float:
    return cosine_similarity([emb1], [emb2])[0][0]
