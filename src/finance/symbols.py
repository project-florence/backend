"""Canonical, source-independent symbol standard + registry.

Design spec section 4: canonical symbols are defined independently of any
data source; providers speak their own contract against them. All translation
metadata lives in exactly one place — the ``SYMBOL_REGISTRY`` below (it is
*symbol metadata*, not provider code). Provider modules must never carry ad
hoc mapping dicts: a provider reads its own raw naming via
``SYMBOL_REGISTRY[sym].provider_symbols[self.name]``.

Naming rules:
- FX symbol = ISO 4217 code quoted in TRY (TRY itself is excluded — it is
  the base currency and is not part of the canonical set).
- Metals = ``{XAU|XAG|XPT|XPD}-{ONS|GRAM}``; Turkish jeweller varieties use
  ``XAU-{CEYREK|YARIM|...}``.
- ``derived_from`` is a derivation *rule* (not a mapping): e.g.
  ``XAU-GRAM = XAU-ONS x USD / 31.1035`` — computed by the service only when
  a direct source did not supply the symbol.
- ``legacy_name`` exists for the transition period only (legacy API keys /
  ticker.py validation).
"""

from dataclasses import dataclass, field

from src.finance.models import AssetClass, ProviderName, QuoteKind

# FX codes quoted in TRY (ISO 4217; TRY excluded — base currency).
FX_CODES: tuple[str, ...] = (
    "USD", "EUR", "GBP", "CHF", "JPY", "AUD", "CAD", "DKK", "SEK", "NOK",
    "RUB", "AED", "KWD", "SAR", "QAR", "BHD", "OMR", "JOD", "PLN", "CZK",
    "HUF", "RON", "BGN", "UAH", "CNY", "HKD", "SGD", "INR", "PKR", "MXN",
    "ZAR", "BRL", "IDR", "MYR", "THB", "PHP", "KRW", "ILS", "EGP", "CLP",
    "ARS", "MAD", "TND", "LBP", "IQD", "LYD", "NZD", "ISK",
)

# TRY pairs that exist on Yahoo (verified live 2026-08-16). Most exotic
# "{CODE}TRY=X" symbols return empty history — probing them is slow and
# throttling-prone, so only verified pairs are registered for yfinance_fx.
YFINANCE_FX_CODES: tuple[str, ...] = ("USD", "EUR", "GBP")

# Symbols quoted as USD per troy ounce (spot/futures reference).
ONS_METALS: tuple[str, ...] = ("XAU-ONS", "XAG-ONS", "XPT-ONS", "XPD-ONS")

# Canonical symbols served by GenelPara's "emtia" (commodity) list.
# (XAU-ONS lives in the "altin" list — emtia has no XAUUSD row.)
_GENELPARA_EMTIA: frozenset[str] = frozenset(
    {"COIL-BRENT-USD", "COIL-WTI-USD", "XAG-ONS", "XPT-ONS", "XPD-ONS"}
)


@dataclass(frozen=True)
class SymbolDef:
    canonical: str            # "XAU-ONS"
    asset_class: AssetClass   # METAL
    quote_kind: QuoteKind     # PRICE | BID_ASK
    currency: str             # "USD" | "TRY"
    unit: str                 # "1 ounce" | "1 gram" | "1 unit"
    legacy_name: str | None = None            # legacy frontend key (transition only)
    provider_symbols: dict = field(default_factory=dict)  # {ProviderName: raw name}
    derived_from: tuple = ()                  # derivation dependencies ("XAU-ONS", "USD")


def _fx_symbol(code: str) -> SymbolDef:
    """FX definition. Raw names are formula-derived (official naming rules):
    GenelPara/TCMB use the ISO code, frankfurter uses the ISO code with a TRY
    base, yfinance uses ``{CODE}TRY=X`` — for the pairs Yahoo actually carries.
    """
    providers = {
        ProviderName.GENELPARA: code,
        ProviderName.TCMB: code,
        ProviderName.FRANKFURTER: code,
    }
    if code in YFINANCE_FX_CODES:
        providers[ProviderName.YFINANCE_FX] = f"{code}TRY=X"
    return SymbolDef(
        canonical=code,
        asset_class=AssetClass.FX,
        quote_kind=QuoteKind.BID_ASK,
        currency="TRY",
        unit="1 unit",
        legacy_name=code,
        provider_symbols=providers,
    )


def _metal_symbol(canonical: str, currency: str, unit: str, legacy: str | None,
                  providers: dict, derived_from: tuple = ()) -> SymbolDef:
    return SymbolDef(
        canonical=canonical,
        asset_class=AssetClass.METAL,
        quote_kind=QuoteKind.PRICE if currency == "USD" else QuoteKind.BID_ASK,
        currency=currency,
        unit=unit,
        legacy_name=legacy,
        provider_symbols=providers,
        derived_from=derived_from,
    )


_SYMBOLS: list[SymbolDef] = [_fx_symbol(code) for code in FX_CODES]

_SYMBOLS += [
    # --- USD-per-ounce references (PRICE) ---------------------------------
    _metal_symbol("XAU-ONS", "USD", "1 ounce", "ons",
                  {ProviderName.GENELPARA: "XAUUSD", ProviderName.YFINANCE_METALS: "GC=F"}),
    _metal_symbol("XAG-ONS", "USD", "1 ounce", None,
                  {ProviderName.GENELPARA: "XAGUSD", ProviderName.YFINANCE_METALS: "SI=F"}),
    _metal_symbol("XPT-ONS", "USD", "1 ounce", None,
                  {ProviderName.GENELPARA: "XPTUSD", ProviderName.YFINANCE_METALS: "PL=F"}),
    _metal_symbol("XPD-ONS", "USD", "1 ounce", None,
                  {ProviderName.GENELPARA: "XPDUSD", ProviderName.YFINANCE_METALS: "PA=F"}),
    # --- TRY gram quotes (direct source or derivation) ---------------------
    _metal_symbol("XAU-GRAM", "TRY", "1 gram", "gram-altin",
                  {ProviderName.GENELPARA: "GA"}, derived_from=("XAU-ONS", "USD")),
    _metal_symbol("XAG-GRAM", "TRY", "1 gram", "gumus",
                  {ProviderName.GENELPARA: "GAG"}, derived_from=("XAG-ONS", "USD")),
    _metal_symbol("XPT-GRAM", "TRY", "1 gram", "gram-platin",
                  {}, derived_from=("XPT-ONS", "USD")),
    _metal_symbol("XPD-GRAM", "TRY", "1 gram", "gram-paladyum",
                  {}, derived_from=("XPD-ONS", "USD")),
    # --- Turkish jeweller varieties (GenelPara only) -----------------------
    _metal_symbol("XAU-HAS", "TRY", "1 gram", "gram-has-altin",
                  {ProviderName.GENELPARA: "XHGLD"}),
    _metal_symbol("XAU-CEYREK", "TRY", "1 unit", "ceyrek-altin",
                  {ProviderName.GENELPARA: "C"}),
    _metal_symbol("XAU-YARIM", "TRY", "1 unit", "yarim-altin",
                  {ProviderName.GENELPARA: "Y"}),
    _metal_symbol("XAU-TAM", "TRY", "1 unit", "tam-altin",
                  {ProviderName.GENELPARA: "T"}),
    _metal_symbol("XAU-CUMHURIYET", "TRY", "1 unit", "cumhuriyet-altini",
                  {ProviderName.GENELPARA: "CMR"}),
    _metal_symbol("XAU-ATA", "TRY", "1 unit", "ata-altin",
                  {ProviderName.GENELPARA: "ATA"}),
    _metal_symbol("XAU-14-AYAR", "TRY", "1 gram", "14-ayar-altin",
                  {ProviderName.GENELPARA: "14"}),
    _metal_symbol("XAU-18-AYAR", "TRY", "1 gram", "18-ayar-altin",
                  {ProviderName.GENELPARA: "18"}),
    _metal_symbol("XAU-22-BILEZIK", "TRY", "1 gram", "22-ayar-bilezik",
                  {ProviderName.GENELPARA: "22"}),
    _metal_symbol("XAU-IKIBUCUK", "TRY", "1 unit", "ikibucuk-altin",
                  {ProviderName.GENELPARA: "IKB"}),
    _metal_symbol("XAU-BESLI", "TRY", "1 unit", "besli-altin",
                  {ProviderName.GENELPARA: "BSL"}),
    _metal_symbol("XAU-GREMSE", "TRY", "1 unit", "gremse-altin",
                  {ProviderName.GENELPARA: "GR"}),
    _metal_symbol("XAU-RESAT", "TRY", "1 unit", "resat-altin",
                  {ProviderName.GENELPARA: "RA"}),
    _metal_symbol("XAU-HAMIT", "TRY", "1 unit", "hamit-altin",
                  {ProviderName.GENELPARA: "HA"}),
    # --- Crude oil (optional, macro) ----------------------------------------
    SymbolDef(
        canonical="COIL-BRENT-USD",
        asset_class=AssetClass.COMMODITY,
        quote_kind=QuoteKind.PRICE,
        currency="USD",
        unit="1 barrel",
        provider_symbols={ProviderName.GENELPARA: "XBRUSD"},
    ),
    SymbolDef(
        canonical="COIL-WTI-USD",
        asset_class=AssetClass.COMMODITY,
        quote_kind=QuoteKind.PRICE,
        currency="USD",
        unit="1 barrel",
        provider_symbols={ProviderName.GENELPARA: "COIL"},
    ),
]

# Canonical symbol -> definition. Order is stable and documented.
SYMBOL_REGISTRY: dict[str, SymbolDef] = {d.canonical: d for d in _SYMBOLS}


def genelpara_category(symbol: str) -> str | None:
    """GenelPara list name for a canonical symbol ("doviz"|"altin"|"emtia").

    The category rule lives here (registry side), keeping GenelPara provider
    code free of symbol mappings. Returns None when the source does not serve
    the symbol.
    """
    d = SYMBOL_REGISTRY.get(symbol)
    if d is None or ProviderName.GENELPARA not in d.provider_symbols:
        return None
    if d.asset_class is AssetClass.FX:
        return "doviz"
    if symbol in _GENELPARA_EMTIA:
        return "emtia"
    return "altin"


def validate_registry() -> list[str]:
    """Registry integrity check (used by tests / startup diagnostics).

    Reports: symbols with no provider and no derivation rule, derivation
    dependencies missing from the registry, and legacy-name collisions.
    """
    problems: list[str] = []
    legacy_seen: dict[str, str] = {}
    for symbol, d in SYMBOL_REGISTRY.items():
        if not d.provider_symbols and not d.derived_from:
            problems.append(f"{symbol}: no provider and no derivation rule")
        for dep in d.derived_from:
            if dep not in SYMBOL_REGISTRY:
                problems.append(f"{symbol}: derived_from dependency {dep} not in registry")
        if d.legacy_name:
            if d.legacy_name in legacy_seen:
                problems.append(
                    f"legacy_name collision: {d.legacy_name} "
                    f"({legacy_seen[d.legacy_name]} vs {symbol})"
                )
            legacy_seen[d.legacy_name] = symbol
    # Every derivation target must have a provider for its ONS/USD legs.
    for symbol, d in SYMBOL_REGISTRY.items():
        if d.derived_from and any(
            not SYMBOL_REGISTRY[dep].provider_symbols for dep in d.derived_from
        ):
            problems.append(f"{symbol}: derivation leg has no direct provider")
    return problems