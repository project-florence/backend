"""Legacy economy bridge — signature protection + all-numeric payload contract.

Design spec 8.1 / Faz 4: every legacy function keeps its exact signature and
delegates to ``FinanceService``. All payloads are numeric — ``Buying`` /
``Selling`` are floats, ``Change`` is ``change_pct`` as a float (never a
comma display string). The frontend "0.00" bug class (plan 8.3) is closed at
the source: when there is no previous close, ``Change`` is ``None`` (renders
"—"), never 0.0 and never "0.00".
"""

import inspect
from datetime import datetime, timezone

import pytest

from src.finance.models import ProviderName, QuoteBundle, Quote
from src.services import economy as legacy

LEGACY_FUNCS = (
    "get_gold_prices",
    "get_silver_price",
    "get_gram_platinum_price",
    "get_gram_palladium_price",
    "get_currency",
    "get_economy_rate_history",
)


def _quote(symbol: str, buying: float, selling: float, change_pct=None) -> Quote:
    return Quote(
        symbol=symbol,
        buying=buying,
        selling=selling,
        change_pct=change_pct,
        source=ProviderName.GENELPARA,
        ts=datetime.now(timezone.utc),
        extra={},
    )


class _FakeFinanceService:
    """FinanceService stand-in serving quotes for requested canonical symbols."""

    def __init__(self, quotes: dict[str, Quote]):
        self._quotes = quotes

    async def get_quotes(self, symbols):
        return QuoteBundle(
            ts=datetime.now(timezone.utc),
            source=ProviderName.GENELPARA,
            quotes={s: self._quotes[s] for s in symbols if s in self._quotes},
            remaining=None,
        )


# ---------------------------------------------------------------------------
# Signature protection
# ---------------------------------------------------------------------------


def test_legacy_functions_exist_as_async_callables():
    for name in LEGACY_FUNCS:
        fn = getattr(legacy, name)
        assert callable(fn)
        assert inspect.iscoroutinefunction(fn), f"{name} must stay async"


def test_legacy_signatures_unchanged():
    assert list(inspect.signature(legacy.get_gold_prices).parameters) == []
    assert list(inspect.signature(legacy.get_silver_price).parameters) == []
    assert list(inspect.signature(legacy.get_gram_platinum_price).parameters) == []
    assert list(inspect.signature(legacy.get_gram_palladium_price).parameters) == []
    assert list(inspect.signature(legacy.get_currency).parameters) == []
    assert list(inspect.signature(legacy.get_economy_rate_history).parameters) == [
        "ticker", "start", "end",
    ]


# ---------------------------------------------------------------------------
# Numeric payloads (FinanceService mocked)
# ---------------------------------------------------------------------------


async def test_get_gold_prices_float_payload_and_0_00_regression(monkeypatch):
    fake = _FakeFinanceService(
        {
            "XAU-ONS": _quote("XAU-ONS", 4021.5, 4025.2, change_pct=1.23),
            "XAU-GRAM": _quote("XAU-GRAM", 2570.0, 2575.0, change_pct=None),
        }
    )
    monkeypatch.setattr(legacy, "finance_service", fake)
    prices = await legacy.get_gold_prices()

    assert set(prices) == {"ons", "gram-altin"}  # legacy keys from the registry
    ons, gram = prices["ons"], prices["gram-altin"]

    # float Buying/Selling
    assert isinstance(ons["Buying"], float) and ons["Buying"] == pytest.approx(4021.5)
    assert isinstance(ons["Selling"], float) and ons["Selling"] == pytest.approx(4025.2)
    # Change is the numeric change_pct
    assert isinstance(ons["Change"], float) and ons["Change"] == pytest.approx(1.23)
    assert isinstance(ons["change_pct"], float)
    assert ons["Type"] == "Gold"
    assert ons["currency"] == "TRY"

    # 0.00 regression: no previous close -> Change is None (frontend shows "—"),
    # never 0.0 and never the old comma string "0,00" / "0.00"
    assert gram["Change"] is None
    assert gram["change_pct"] is None
    assert "change_text" not in gram  # display hint only exists with a real change

    # no comma/display strings anywhere in the numeric fields
    for entry in prices.values():
        assert not isinstance(entry["Buying"], str)
        assert not isinstance(entry["Selling"], str)
        assert entry["Change"] is None or isinstance(entry["Change"], float)
        assert "0,00" not in str(entry)
        assert "0.00" not in str(entry.get("Change"))


async def test_get_currency_numeric(monkeypatch):
    fake = _FakeFinanceService(
        {
            "USD": _quote("USD", 40.5, 40.6, change_pct=0.5),
            "EUR": _quote("EUR", 44.5, 44.6, change_pct=None),
        }
    )
    monkeypatch.setattr(legacy, "finance_service", fake)
    currencies = await legacy.get_currency()

    assert {"USD", "EUR"} <= set(currencies)
    assert currencies["USD"]["Type"] == "Currency"
    assert isinstance(currencies["USD"]["Buying"], float)
    assert currencies["USD"]["Change"] == pytest.approx(0.5)
    assert currencies["EUR"]["Change"] is None  # 0.00 regression, same rule


async def test_gram_metal_legacy_keys_and_types(monkeypatch):
    fake = _FakeFinanceService(
        {
            "XAG-GRAM": _quote("XAG-GRAM", 30.0, 30.2),
            "XPT-GRAM": _quote("XPT-GRAM", 950.0, 955.0),
            "XPD-GRAM": _quote("XPD-GRAM", 980.0, 985.0),
        }
    )
    monkeypatch.setattr(legacy, "finance_service", fake)

    silver = await legacy.get_silver_price()
    pt = await legacy.get_gram_platinum_price()
    pd = await legacy.get_gram_palladium_price()

    assert set(silver) == {"gumus"} and silver["gumus"]["Buying"] == pytest.approx(30.0)
    assert silver["gumus"]["Type"] == "Gold"
    assert set(pt) == {"gram-platin"} and pt["gram-platin"]["Buying"] == pytest.approx(950.0)
    assert pt["gram-platin"]["Type"] == "Commodity"
    assert set(pd) == {"gram-paladyum"} and pd["gram-paladyum"]["Buying"] == pytest.approx(980.0)


async def test_stale_flag_and_db_snapshot_source_propagate(monkeypatch):
    q = _quote("XAU-ONS", 4000.0, 4010.0)
    q.stale = True
    q.source = ProviderName.DB_SNAPSHOT
    fake = _FakeFinanceService({"XAU-ONS": q})
    monkeypatch.setattr(legacy, "finance_service", fake)

    prices = await legacy.get_gold_prices()
    assert prices["ons"]["stale"] is True
    assert prices["ons"]["source"] == "db_snapshot"