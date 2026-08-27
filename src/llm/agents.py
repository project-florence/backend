"""Amac-bazli pydantic-ai model kurucusu -- digest ve raporun ortak baglantisi.

REFACTOR_PLAN.md Adim 2: ``build_agent(purpose)`` ``src.llm.settings.resolve_purpose``
ile saglayici + model + (cozulmus) base_url + (cozulmus) API anahtari + params
alir, ``api_style``'a gore uygun pydantic-ai provider/model'ini kurar ve
reasoning ayarini (varsa) hazirlar.

``build_agent`` ZORUNLU olarak async: ``resolve_purpose`` (bkz. ``src/llm/settings.py``)
``src/core/database.py``'deki ``AsyncConnectionPool`` (satir 27, 70) ve
``src/core/redis.py``'deki ``aioredis.Redis`` (satir 41) singleton'larina dokunuyor.
Bu ikisi de ana event loop'ta kuruluyor ve o loop'a bagli -- baska bir thread'de
kendi basina yeni bir loop acip bunlari kullanmak psycopg_pool ve async redis'te
desteklenmiyor ("attached to a different loop" hatasi veya kilitlenme). Bir
onceki surumde tam olarak bu hataya dusen bir thread-havuzu koprusu vardi;
buraya BENZER bir senkron kopru YENIDEN KURULMASIN -- cagiran
taraflar (``digest/service.py::generate_digest``, ``report/__init__.py::generate_report``)
zaten async baglamda calisiyor, dolayisiyla ``await`` etmek dogru ve yeterli
cozum.

Reasoning kurali (REFACTOR_PLAN.md 2.5, PROVIDERS.md "reasoning_param neden
bazilarinda bos"): ``output_type`` bir pydantic modeli olan amaclarda
(``structured_output_forbids_reasoning``) reasoning VARSAYILAN olarak
kapalidir -- parametre HIC GONDERILMEZ (bir "kapali" degeri degil, anahtarin
kendisi model_settings'te hic yer almaz). ``llm_settings.params.reasoning``
acikca ayarlanmissa bu varsayilan gecersiz kilinir (admin'in bilincli
tercihi). Saglayicinin ``reasoning_param``'i ``None`` ise (gateway/proxy
saglayicilar, sabit CoT modelleri -- bkz. PROVIDERS.md) reasoning HICBIR
ZAMAN gonderilmez. Model adina bakan sezgi
(``"deepseek" not in model_name.lower()``) burada TAMAMEN YOK -- 2026-08-26
arizasinin kok nedeni buydu.

Gozlemlenebilirlik (REFACTOR_PLAN.md Adim 3): 2026-08-26 arizasinin asil
kalici zarari model/URL uyumsuzlugu degil, bunun HICBIR YERE
yazilmamasiydi -- digest ``token_usage``'a hic yazmiyordu, hata yalniz
ucucu container logundaydi. ``log_llm_call`` bu modulde tanimlanir (cagiran
``digest/service.py`` ve ``report/__init__.py`` olsa da) cunku sir
temizleme (``_sanitize_error``) LLM cagri hatalarina ozgu bir kaygi --
API anahtari, Authorization basligi ya da base_url kimlik bilgisi hata
metninde ASLA DB'ye yazilmamali. ``log_llm_call`` kendi ic hatasini (ornegin
DB dususe) YUTAR ve ``logger.warning`` ile gecer -- loglama yolu ana LLM
cagrisini asla dusurmemeli.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from src.llm.settings import (
    LLMPurposeUnconfigured,
    ResolvedLLM,
    Unconfigured,
    resolve_purpose,
    structured_output_forbids_reasoning,
)

logger = logging.getLogger(__name__)

# pydantic-ai'nin Anthropic model_settings'inde reasoning "effort" seviyesi
# (low/medium/high) dogrudan ``anthropic_effort`` alaniyla ifade ediliyor --
# ``anthropic_thinking`` (budget_tokens tabanli, farkli bir sekil) degil.
# Katalogdaki ``reasoning_param="anthropic_thinking"`` ismi PROVIDERS.md'de
# soyut bir isimlendirme olarak birakildi; burada gercek pydantic-ai alanina
# eslenir.
_ANTHROPIC_EFFORT_SETTING = "anthropic_effort"


def _reasoning_model_settings(resolved: ResolvedLLM, purpose: str) -> dict[str, Any]:
    """Reasoning ayarini (varsa) model_settings sozlugune cevirir.

    Varsayilan davranis HICBIR SEY GONDERMEMEK (bkz. modul docstring'i).
    """
    provider = resolved.provider
    if provider.reasoning_param is None:
        return {}

    override = resolved.params.get("reasoning") if isinstance(resolved.params, dict) else None
    if override is None:
        return {}

    if structured_output_forbids_reasoning(purpose):
        logger.warning(
            "purpose=%r uses structured output (reasoning is off by default) but "
            "llm_settings.params.reasoning=%r is explicitly set; honoring the "
            "admin override.",
            purpose,
            override,
        )

    if provider.reasoning_values and override not in provider.reasoning_values:
        logger.warning(
            "reasoning=%r is not valid for provider %r (accepted: %s); dropping "
            "the reasoning setting instead of sending an unsupported value.",
            override,
            provider.id,
            sorted(provider.reasoning_values),
        )
        return {}

    if provider.reasoning_param == "openai_reasoning_effort":
        return {"openai_reasoning_effort": override}
    if provider.reasoning_param == "anthropic_thinking":
        return {_ANTHROPIC_EFFORT_SETTING: override}
    if provider.reasoning_param == "openrouter_reasoning":
        return {"extra_body": {"reasoning": {"effort": override}}}

    logger.warning(
        "unknown reasoning_param=%r for provider=%r; dropping the reasoning "
        "setting instead of guessing a shape for it.",
        provider.reasoning_param,
        provider.id,
    )
    return {}


@dataclass(frozen=True)
class BuiltAgent:
    """``build_agent()`` sonucu: hazir pydantic-ai modeli + model_settings.

    ``model_name`` / ``provider_id`` sadece gunlukleme ve denetim (audit) icin
    -- cagiran taraf bunlari token_usage kaydina yazmak icin kullanabilir
    (bkz. REFACTOR_PLAN.md Adim 3).
    """

    model: Model
    model_settings: dict[str, Any]
    model_name: str
    provider_id: str


async def build_agent(purpose: str) -> BuiltAgent:
    """Bir amac icin pydantic-ai modeli + model_settings kurar.

    Cozumlemeyi ``src.llm.settings.resolve_purpose``'a devreder. Amac
    yapilandirilmamissa/cozulemiyorsa ``LLMPurposeUnconfigured`` firlatir --
    sessizce varsayilana dusmek YOK.
    """
    resolved = await resolve_purpose(purpose)
    if isinstance(resolved, Unconfigured):
        raise LLMPurposeUnconfigured(resolved)
    provider = resolved.provider
    # opencode-zen/opencode-go/ollama-local gibi bazi saglayicilar auth
    # istemiyor (bkz. PROVIDERS.md); OpenAIProvider yine de bir api_key
    # bekliyor, bu yuzden bos degilse kullanicinin gercek anahtari, degilse
    # zararsiz bir yer tutucu gonderiliyor -- bu bir "gizli varsayilana
    # dusme" DEGIL, sadece SDK'nin zorunlu alanini doldurma.
    api_key = resolved.api_key or "not-needed"

    if provider.api_style == "openai-chat":
        model: Model = OpenAIChatModel(
            resolved.model,
            provider=OpenAIProvider(base_url=resolved.base_url, api_key=api_key),
        )
    elif provider.api_style == "anthropic":
        model = AnthropicModel(
            resolved.model,
            provider=AnthropicProvider(base_url=resolved.base_url, api_key=api_key),
        )
    else:
        raise ValueError(
            f"api_style {provider.api_style!r} (provider={provider.id!r}) is not "
            "yet implemented in build_agent()"
        )

    return BuiltAgent(
        model=model,
        model_settings=_reasoning_model_settings(resolved, purpose),
        model_name=resolved.model,
        provider_id=provider.id,
    )


# ---------------------------------------------------------------------------
# Gozlemlenebilirlik (REFACTOR_PLAN.md Adim 3)
# ---------------------------------------------------------------------------

_MAX_ERROR_LEN = 500

# Sir redaksiyonu -- httpx/openai istisnalari bazen istegin baslıklarini/
# URL'ini `str(exc)` icine gomer (ornegin bir 401/403 govdesi istek ozetiyle
# birlikte donuyorsa). Buradaki regex'ler bilinen sekilleri (Authorization
# basligi, Bearer token, api_key=..., sk-... stili anahtarlar, URL'e gomulu
# kimlik bilgisi) DB'ye yazilmadan once temizler. Kor bir `str(exc)` YETERLI
# DEGIL -- bu yuzden CLI'nin de gercek anahtari hicbir zaman basmamasi
# (bkz. src/llm/settings.py::mask_secret) ayri, bagimsiz bir savunma
# katmanidir; burasi tek savunma degil.
_AUTHORIZATION_KV_RE = re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^'\"\n\r]+")
_BEARER_RE = re.compile(r"(?i)bearer\s+[^\s'\"]+")
_APIKEY_KV_RE = re.compile(r"(?i)((?:api[_-]?key|x-api-key)['\"]?\s*[:=]\s*['\"]?)[^\s'\"&,]+")
_SK_TOKEN_RE = re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_\-]{6,}\b")
_URL_CREDENTIALS_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def _sanitize_error(exc: BaseException) -> str:
    """``token_usage.error``'a yazilacak kisa, sirsiz hata metni.

    Bicim: ``"<IstisnaSinifi>: <kisaltilmis mesaj>"``, en fazla
    ``_MAX_ERROR_LEN`` karakter. Bilinmeyen/gorulmemis bir sir sekli
    kacabilir -- bu fonksiyon "iyi niyetli" bir filtre, tek savunma hatti
    degil.
    """
    text = f"{type(exc).__name__}: {exc}"
    text = _AUTHORIZATION_KV_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _APIKEY_KV_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _SK_TOKEN_RE.sub("[REDACTED]", text)
    text = _URL_CREDENTIALS_RE.sub("://[REDACTED]@", text)
    return text[:_MAX_ERROR_LEN]


async def log_llm_call(
    *,
    purpose: str,
    model_name: str,
    provider_id: str | None,
    status: str,
    duration_ms: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    error: BaseException | str | None = None,
    user_id: int | None = None,
) -> None:
    """Bir LLM cagrisini (basari veya hata) ``token_usage``'a kaydeder.

    Cagiranlar (``digest/service.py::generate_digest``,
    ``report/__init__.py::generate_report``) bunu hem ``agent.run()``
    basarili donduğunde hem de bir istisna yakaladiklarinda cagirir --
    ikinci durumda ``status="error"`` ve ``error`` (istisnanin kendisi;
    burada sanitize edilir) ile. Bu fonksiyon KENDI hatasini asla yukari
    firlatmaz -- DB/Redis dususu gibi bir loglama arizasi gercek LLM
    cagrisini/digest uretimini asla dusurmemeli (REFACTOR_PLAN.md Adim 3
    "Kesin kurallar"); yalniz ``logger.warning`` ile gecilir.
    """
    try:
        from src.services.token import log_token_usage

        error_text: str | None
        if error is None:
            error_text = None
        elif isinstance(error, str):
            error_text = error[:_MAX_ERROR_LEN]
        else:
            error_text = _sanitize_error(error)

        await log_token_usage(
            model=model_name,
            purpose=purpose,
            provider=provider_id,
            status=status,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            duration_ms=duration_ms,
            error=error_text,
            user_id=user_id,
        )
    except Exception as exc:
        logger.warning(
            "token_usage kaydi basarisiz oldu (purpose=%r, status=%r); LLM "
            "cagrisinin kendisi bundan etkilenmiyor: %s",
            purpose,
            status,
            exc,
        )


def elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
