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
"""

import logging
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
