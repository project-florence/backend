"""LLM saglik probu.

Bu modul artik gercek trafik icin KULLANILMIYOR -- digest, rapor ve gomme
``src.llm.agents.build_agent`` / ``src.clients.embedding`` uzerinden
dogrudan ``src.llm.settings.resolve_purpose``'a baglidir (REFACTOR_PLAN.md
Adim 2). Bu dosyanin tek cagirani ``src/admin/__init__.py::healthcheck`` ve
``scripts/doctor.py::check_llm``.

2026-08-26 arizasinin bir parcasi da buydu: eskiden bu modul kendi ayri env
degiskenleri uzerinden, digest'in GERCEKTEN kullandigi yapilandirmadan
TAMAMEN AYRI bir sey test ediyordu -- digest'in yapilandirmasi
bozulduktan sonra bile saglik kontrolu yesil kalmaya devam etti, cunku farkli
bir (hala calisan) sagliayiciyi yokluyordu. ``health_check()`` artik
``PURPOSES`` icindeki her amaci ``resolve_purpose`` ile cozup GERCEKTEN o
saglayiciya canli, hafif bir istek atar; boylece saglik kontrolu digest/
rapor/gomme'nin fiilen kullandigi seyi test eder.
"""

import logging

import httpx

from src.clients.http import get_client
from src.llm.settings import PURPOSES, ResolvedLLM, resolve_purpose

logger = logging.getLogger(__name__)


async def _probe_provider(resolved: ResolvedLLM) -> bool:
    """Cozulmus bir amacin saglayicisina canli, hafif bir istek atar.

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
    """En az bir amac (digest/report/embedding) yapilandirilmis VE saglikli mi?

    Hicbir amac yapilandirilmamissa (temiz kurulum, REFACTOR_PLAN.md Adim 7'nin
    bilincli "kisa yapilandirilmamis pencere"si) ``False`` doner -- sessiz
    basari YOK. Yapilandirilmis amaclardan biri bile saglaniyorsa (canli
    probu gecerse) genel sonuc olumsuz sayilir.
    """
    any_configured = False
    all_healthy = True
    for purpose in PURPOSES:
        resolved = await resolve_purpose(purpose)
        if not isinstance(resolved, ResolvedLLM):
            continue
        any_configured = True
        if not await _probe_provider(resolved):
            logger.warning(
                "LLM health check failed for purpose=%s provider=%s model=%s",
                purpose,
                resolved.provider.id,
                resolved.model,
            )
            all_healthy = False
    return any_configured and all_healthy
