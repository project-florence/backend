"""Symbol registry integrity (design spec section 4)."""

import re

from src.finance.models import AssetClass, ProviderName, QuoteKind
from src.finance.providers.registry import PROVIDERS
from src.finance.symbols import FX_CODES, SYMBOL_REGISTRY, validate_registry

# 48 FX + 4 ONS + 4 GRAM + 1 HAS + 13 jeweller varieties + 2 commodities.
EXPECTED_SYMBOL_COUNT = 72


def test_validate_registry_reports_no_problems():
    assert validate_registry() == []


def test_registry_size_and_shape():
    assert len(SYMBOL_REGISTRY) == EXPECTED_SYMBOL_COUNT
    for canonical, d in SYMBOL_REGISTRY.items():
        assert d.canonical == canonical
        assert d.asset_class in AssetClass
        assert d.quote_kind in QuoteKind
        assert d.currency in {"TRY", "USD"}
        assert d.unit
        # Every canononical symbol is served by at least one provider or a
        # derivation rule (validate_registry's own invariant, asserted here).
        assert d.provider_symbols or d.derived_from
        assert (d.currency == "USD") is (d.quote_kind is QuoteKind.PRICE)


def test_provider_symbols_reference_real_providers():
    for canonical, d in SYMBOL_REGISTRY.items():
        for name, raw in d.provider_symbols.items():
            assert isinstance(name, ProviderName)
            assert isinstance(raw, str) and raw
            assert name in PROVIDERS, f"{canonical}: unknown provider {name}"


def test_legacy_names_unique():
    seen: dict[str, str] = {}
    for canonical, d in SYMBOL_REGISTRY.items():
        if d.legacy_name:
            assert d.legacy_name not in seen, f"legacy collision: {d.legacy_name}"
            seen[d.legacy_name] = canonical


def test_fx_codes_are_valid_iso():
    assert len(FX_CODES) == len(set(FX_CODES))  # no duplicates
    for code in FX_CODES:
        assert re.fullmatch(r"[A-Z]{3}", code), f"not an ISO code: {code}"
        assert code != "TRY"  # TRY is the base currency, never canonical
        d = SYMBOL_REGISTRY[code]
        assert d.asset_class is AssetClass.FX
        assert d.quote_kind is QuoteKind.BID_ASK
        assert d.currency == "TRY"
        assert d.legacy_name == code
        # Every FX code is registered with its 3 main providers.
        for name in (ProviderName.GENELPARA, ProviderName.TCMB, ProviderName.FRANKFURTER):
            assert name in d.provider_symbols


def test_derivation_rules_reference_existing_symbols():
    for canonical, d in SYMBOL_REGISTRY.items():
        for dep in d.derived_from:
            assert dep in SYMBOL_REGISTRY, f"{canonical}: dep {dep} missing"
        if d.derived_from:
            # Derived grams are TRY bid/ask quotes; their legs must exist.
            assert d.currency == "TRY"
            assert d.quote_kind is QuoteKind.BID_ASK
            for dep in d.derived_from:
                assert SYMBOL_REGISTRY[dep].provider_symbols


def test_gram_derivation_rules_are_the_documented_four():
    """The four gram derivations are XAU/XAG/XPT/XPD-GRAM = ONS x USD / 31.1035."""
    expected = {
        "XAU-GRAM": ("XAU-ONS", "USD"),
        "XAG-GRAM": ("XAG-ONS", "USD"),
        "XPT-GRAM": ("XPT-ONS", "USD"),
        "XPD-GRAM": ("XPD-ONS", "USD"),
    }
    for symbol, deps in expected.items():
        d = SYMBOL_REGISTRY[symbol]
        assert d.derived_from == deps
        assert d.unit == "1 gram"
        assert d.currency == "TRY"