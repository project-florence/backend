"""Gram derivation — XAU-GRAM = XAU-ONS x USD / 31.1035 (design spec 2.6).

The rule runs only when a direct source did not supply the gram symbol in
the same collection round; a direct quote always wins over derivation.
"""

from datetime import datetime, timezone

import pytest
import respx
from httpx import Response

from src.finance import FinanceService
from src.finance.models import ProviderName
from tests.helpers import (
    FRANKFURTER_URL,
    GA_ITEM,
    GENELPARA_URL,
    TCMB_URL,
    USD_ITEM,
    XAUUSD_ITEM,
    make_quote,
    mock_storage,
)

GRAM_PER_OZ = 31.1035  # config default (finance.gram_per_oz)

_NOW = datetime.now(timezone.utc)


def _ons_quote():
    return make_quote("XAU-ONS", buying=2000.0, price=2000.0, source=ProviderName.GENELPARA)


def _usd_quote():
    return make_quote("USD", buying=40.0, selling=40.1, source=ProviderName.GENELPARA)


# ---------------------------------------------------------------------------
# _derive_grams unit level
# ---------------------------------------------------------------------------


def test_derive_grams_unit_rule():
    quotes = {"XAU-ONS": _ons_quote(), "USD": _usd_quote()}
    FinanceService()._derive_grams(quotes)

    gram = quotes.get("XAU-GRAM")
    assert gram is not None
    expected = 2000.0 * 40.0 / GRAM_PER_OZ
    assert gram.buying == pytest.approx(expected)
    assert gram.selling == pytest.approx(expected)
    assert gram.price == pytest.approx(expected)
    assert gram.currency == "TRY" and gram.unit == "1 gram"
    assert gram.extra["derived"] is True
    assert gram.extra["formula"] == "XAU-ONS x USD / 31.1035"
    assert gram.source is ProviderName.GENELPARA  # audit: ounce-leg winner
    assert quotes["XAU-ONS"].extra == {}  # source quotes untouched


def test_derive_grams_does_not_overwrite_direct_quote():
    direct = make_quote("XAU-GRAM", buying=2600.0, selling=2605.0)
    quotes = {"XAU-ONS": _ons_quote(), "USD": _usd_quote(), "XAU-GRAM": direct}
    FinanceService()._derive_grams(quotes)

    assert quotes["XAU-GRAM"].buying == pytest.approx(2600.0)
    assert quotes["XAU-GRAM"].extra.get("derived") is not True


def test_derive_grams_skips_when_legs_missing():
    service = FinanceService()
    quotes = {"XAU-ONS": _ons_quote()}  # no USD leg
    service._derive_grams(quotes)
    assert "XAU-GRAM" not in quotes

    quotes2 = {"USD": _usd_quote()}  # no ONS leg
    service._derive_grams(quotes2)
    assert "XAU-GRAM" not in quotes2

    quotes3 = {}  # nothing at all
    service._derive_grams(quotes3)
    assert quotes3 == {}


# ---------------------------------------------------------------------------
# Service level (respx-mocked genelpara)
# ---------------------------------------------------------------------------


def _mock_genelpara_dispatch(altin_data: dict, doviz_data: dict):
    def handler(request):
        if request.url.params["list"] == "doviz":
            return Response(
                200, json={"success": True, "remaining": 800, "data": doviz_data}
            )
        if request.url.params["list"] == "altin":
            return Response(
                200, json={"success": True, "remaining": 800, "data": altin_data}
            )
        return Response(500)

    return handler


async def test_service_derives_gram_when_direct_source_missing(monkeypatch):
    mock_storage(monkeypatch)
    with respx.mock:
        # altin list carries XAUUSD but NO "GA" row -> gram must be derived
        respx.get(url__startswith=GENELPARA_URL).mock(
            side_effect=_mock_genelpara_dispatch(
                altin_data={"XAUUSD": XAUUSD_ITEM}, doviz_data={"USD": USD_ITEM}
            )
        )
        respx.get(TCMB_URL).mock(return_value=Response(500))
        respx.get(FRANKFURTER_URL).mock(return_value=Response(500))
        bundle = await FinanceService().refresh_symbols({"XAU-ONS", "USD", "XAU-GRAM"})

    assert set(bundle.quotes) == {"XAU-ONS", "USD", "XAU-GRAM"}
    gram = bundle.quotes["XAU-GRAM"]
    expected = 2000.0 * 40.5210 / GRAM_PER_OZ  # USD_ITEM alis = 40.5210
    assert gram.buying == pytest.approx(expected)
    assert gram.extra.get("derived") is True
    assert gram.source is ProviderName.GENELPARA
    assert bundle.quotes["XAU-ONS"].price == pytest.approx(2000.00)
    assert bundle.quotes["USD"].buying == pytest.approx(40.5210)


async def test_service_direct_ga_wins_over_derivation(monkeypatch):
    mock_storage(monkeypatch)
    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(
            side_effect=_mock_genelpara_dispatch(
                altin_data={"XAUUSD": XAUUSD_ITEM, "GA": GA_ITEM},
                doviz_data={"USD": USD_ITEM},
            )
        )
        respx.get(TCMB_URL).mock(return_value=Response(500))
        respx.get(FRANKFURTER_URL).mock(return_value=Response(500))
        bundle = await FinanceService().refresh_symbols({"XAU-ONS", "USD", "XAU-GRAM"})

    gram = bundle.quotes["XAU-GRAM"]
    assert gram.buying == pytest.approx(2570.00)  # direct GA row, not derived
    assert "derived" not in gram.extra
    assert gram.extra.get("yon") == "moneyUp"