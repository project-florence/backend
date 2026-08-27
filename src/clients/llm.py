"""LLM saglik probu.

Bu modul artik gercek trafik icin KULLANILMIYOR -- digest ve rapor
``src.llm.agents.build_agent`` uzerinden dogrudan ``src.llm.settings.
resolve_llm``'e baglidir (REFACTOR_PLAN.md Adim 2, Adim 6.5). Bu dosyanin
tek cagirani ``src/admin/__init__.py::healthcheck`` ve
``scripts/doctor.py``.

2026-08-26 arizasinin bir parcasi da buydu: eskiden bu modul kendi ayri env
degiskenleri uzerinden, digest'in GERCEKTEN kullandigi yapilandirmadan
TAMAMEN AYRI bir sey test ediyordu -- digest'in yapilandirmasi
bozulduktan sonra bile saglik kontrolu yesil kalmaya devam etti, cunku farkli
bir (hala calisan) sagliayiciyi yokluyordu. ``health_check()`` artik TEK
ayari (Adim 6.5: amac-basina degil singleton -- digest ve report AYNI
saglayici/modeli paylasir) ``resolve_llm`` ile cozup GERCEKTEN o
saglayiciya canli, hafif bir istek atar; boylece saglik kontrolu digest/
rapor'un fiilen kullandigi seyi test eder.
"""

import logging

import httpx

from src.clients.http import get_client
from src.llm.settings import ResolvedLLM, resolve_llm

logger = logging.getLogger(__name__)


async def _probe_provider(resolved: ResolvedLLM) -> bool:
    """Cozulmus TEK ayarin saglayicisina canli, hafif bir istek atar.

    ``models_url`` yoksa (ornegin ``openai-compatible``/``ollama-local`` gibi
    ozel kurulumlarda katalogda tanimli bir canli roster ucnoktasi yoktur)
    yapilandirmanin cozulebilir olmasi yeterli kabul edilir -- agdan test
    edilmez.
    """
    provider = resolved.provider
    if not provider.models_url:
        return True

    headers: dict[str, str] = {}
    if resolved.api_key:
        if provider.api_style == "anthropic":
            headers["x-api-key"] = resolved.api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {resolved.api_key}"

    try:
        client = await get_client()
        response = await client.get(provider.models_url, headers=headers, timeout=10)
        # 401/403 (gecersiz/reddedilen anahtar) gercek bir arizadir; 4xx'in
        # geri kalani (ornegin bazi gateway'lerin GET /models'a verdigi
        # beklenmedik ama sunucu-tarafi olmayan yanitlar) saglayicinin
        # ulasilabilir oldugunu gosterir.
        return response.status_code < 500 and response.status_code not in (401, 403)
    except httpx.HTTPError as e:
        logger.warning("LLM health probe failed for provider=%s: %s", provider.id, e)
        return False


async def health_check() -> bool:
    """TEK LLM ayari (digest+report paylasir) yapilandirilmis VE saglikli mi?

    Yapilandirilmamissa (temiz kurulum, REFACTOR_PLAN.md Adim 7'nin bilincli
    "kisa yapilandirilmamis pencere"si) ``False`` doner -- sessiz basari YOK.
    """
    resolved = await resolve_llm()
    if not isinstance(resolved, ResolvedLLM):
        return False
    healthy = await _probe_provider(resolved)
    if not healthy:
        logger.warning(
            "LLM health check failed for provider=%s model=%s",
            resolved.provider.id,
            resolved.model,
        )
    return healthy
