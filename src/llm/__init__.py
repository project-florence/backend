"""LLM saglayici/model altyapisi.

REFACTOR_PLAN.md'de tanimlanan katman: sifreleme (``crypto``), saglayici
katalogu (``providers``), TEK ayarin okuma/yazma + cozumlemesi (``settings``,
Adim 6.5: amac-basina degil singleton) ve amac-etiketli pydantic-ai model
kurucusu (``agents``). Digest (``src/services/digest/agent.py``) ve rapor
(``src/services/report/__init__.py``) Adim 2'de bu katmana baglandi; eski
ozel LLM ortam degiskeni yollarinin tamami kaldirildi.

Embedding (Adim 6.5.B) bu katmandan TAMAMEN CIKARILDI: embedding bir LLM
degil ve ``src/clients/embedding.py``'nin hicbir cagirani yoktu (dogrulandi)
-- var olmayan bir tuketici icin yapilandirma yuzeyiydi. Dosya silindi.
Sayisal ozellik vektorlerini (``stock_vectors`` tablosu) dolduran
``src/analysis/stock_vector.py`` bununla ILGISIZ, bu bir LLM embedding'i
degil.
"""

from src.llm.agents import BuiltAgent, build_agent
from src.llm.crypto import (
    DecryptionFailed,
    LLMCryptoError,
    MasterKeyInvalid,
    MasterKeyMissing,
)
from src.llm.providers import PROVIDERS, InvalidModelSpec, ProviderSpec, resolve
from src.llm.settings import (
    PURPOSES,
    LLMPurposeUnconfigured,
    ResolvedLLM,
    Unconfigured,
    resolve_llm,
    structured_output_forbids_reasoning,
)

__all__ = [
    "PROVIDERS",
    "PURPOSES",
    "BuiltAgent",
    "DecryptionFailed",
    "InvalidModelSpec",
    "LLMCryptoError",
    "LLMPurposeUnconfigured",
    "MasterKeyInvalid",
    "MasterKeyMissing",
    "ProviderSpec",
    "ResolvedLLM",
    "Unconfigured",
    "build_agent",
    "resolve",
    "resolve_llm",
    "structured_output_forbids_reasoning",
]
