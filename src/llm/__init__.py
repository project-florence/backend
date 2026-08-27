"""LLM saglayici/model altyapisi.

REFACTOR_PLAN.md'de tanimlanan katman: sifreleme (``crypto``), saglayici
katalogu (``providers``), ayar okuma/yazma + cozumleme (``settings``) ve
amac-bazli pydantic-ai model kurucusu (``agents``). Digest
(``src/services/digest/agent.py``), rapor (``src/services/report/__init__.py``)
ve gomme (``src/clients/embedding.py``) Adim 2'de bu katmana baglandi; eski
ozel LLM/gomme ortam degiskeni yollarinin tamami kaldirildi.
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
    resolve_purpose,
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
    "resolve_purpose",
    "structured_output_forbids_reasoning",
]
