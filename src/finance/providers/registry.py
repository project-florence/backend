"""Provider capabilities + fallback priority (design spec 2.6 / 3.4).

Provider instances are built once here and each gets its ``provides`` set
derived from the symbol registry (``provider_symbols`` keys). ``chains_for``
returns the ordered fallback chain for a canonical symbol — the service walks
the chain and takes the first quote that came back. ``DB_SNAPSHOT`` is a
service-level last resort (read from ``storage``), not a provider instance.
"""

from src.finance.models import AssetClass, ProviderName
from src.finance.providers.base import BaseProvider
from src.finance.providers.frankfurter import FrankfurterProvider
from src.finance.providers.genelpara import GenelParaProvider
from src.finance.providers.tcmb import TcmbProvider
from src.finance.providers.yfinance_fx import YFinanceFxProvider
from src.finance.providers.yfinance_metals import YFinanceMetalsProvider
from src.finance.symbols import ONS_METALS, SYMBOL_REGISTRY

# Ordered fallback chains (highest priority first) — design spec 2.6.
FX_CHAIN = (
    ProviderName.GENELPARA,
    ProviderName.TCMB,
    ProviderName.FRANKFURTER,
    ProviderName.YFINANCE_FX,
)
ONS_CHAIN = (ProviderName.GENELPARA, ProviderName.YFINANCE_METALS)
# TRY jeweller varieties and commodities have GenelPara as their only live
# provider; DB_SNAPSHOT is a service-level (not fetched) last resort so a
# GenelPara outage degrades to a stale value instead of the symbol vanishing.
VARIETY_CHAIN = (ProviderName.GENELPARA, ProviderName.DB_SNAPSHOT)
COMMODITY_CHAIN = (ProviderName.GENELPARA, ProviderName.DB_SNAPSHOT)

_ONS_SET = frozenset(ONS_METALS)


def chains_for(symbol: str) -> tuple:
    """Ordered provider chain for a canonical symbol (empty for unknown)."""
    d = SYMBOL_REGISTRY.get(symbol)
    if d is None:
        return ()
    if d.asset_class is AssetClass.FX:
        return FX_CHAIN
    if d.asset_class is AssetClass.COMMODITY:
        return COMMODITY_CHAIN
    if symbol in _ONS_SET:
        return ONS_CHAIN
    return VARIETY_CHAIN


def _build_providers() -> dict[ProviderName, BaseProvider]:
    instances: dict[ProviderName, BaseProvider] = {
        ProviderName.GENELPARA: GenelParaProvider(),
        ProviderName.TCMB: TcmbProvider(),
        ProviderName.FRANKFURTER: FrankfurterProvider(),
        ProviderName.YFINANCE_FX: YFinanceFxProvider(),
        ProviderName.YFINANCE_METALS: YFinanceMetalsProvider(),
    }
    for name, instance in instances.items():
        instance.provides = frozenset(
            symbol
            for symbol, d in SYMBOL_REGISTRY.items()
            if name in d.provider_symbols
        )
    return instances


PROVIDERS: dict[ProviderName, BaseProvider] = _build_providers()


def provider(name: ProviderName) -> BaseProvider | None:
    """Provider instance by name (None for service-level DB_SNAPSHOT)."""
    return PROVIDERS.get(name)


def all_providers() -> list[BaseProvider]:
    return list(PROVIDERS.values())