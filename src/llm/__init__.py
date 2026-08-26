"""LLM saglayici/model altyapisi.

REFACTOR_PLAN.md'de tanimlanan katmanin temeli (Adim 1). Bu asamada yalnizca
temel yapi taslari var: sifreleme (``crypto``), saglayici katalogu
(``providers``) ve ayar okuma/yazma + cozumleme (``settings``). Digest,
rapor ve gomme (embedding) akislari HENUZ bu katmana baglanmadi -- hala eski
``CUSTOM_*``/``LLM_CLIENT_*`` env yolundan calisiyorlar (Adim 2'nin isi).

Amac-bazli ajan insasi (``build_agent(purpose)``) Adim 2'de ``src/llm/agents.py``
olarak eklenecek; bu paket o zamana kadar hicbir yere baglanmaz.
"""

from src.llm.crypto import (
    DecryptionFailed,
    LLMCryptoError,
    MasterKeyInvalid,
    MasterKeyMissing,
)
from src.llm.providers import PROVIDERS, InvalidModelSpec, ProviderSpec, resolve
from src.llm.settings import (
    PURPOSES,
    ResolvedLLM,
    Unconfigured,
    resolve_purpose,
    structured_output_forbids_reasoning,
)

__all__ = [
    "PROVIDERS",
    "PURPOSES",
    "DecryptionFailed",
    "InvalidModelSpec",
    "LLMCryptoError",
    "MasterKeyInvalid",
    "MasterKeyMissing",
    "ProviderSpec",
    "ResolvedLLM",
    "Unconfigured",
    "resolve",
    "resolve_purpose",
    "structured_output_forbids_reasoning",
]
