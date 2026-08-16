"""GenelPara provider — new API envelope parsing (success/list/remaining/data).

Fixtures mirror the contract verified live 2026-08-16: every item field is a
string (``alis``/``satis``/``degisim``...), the envelope carries ``remaining``
(daily quota) and the provider must translate all of it into canonical Quotes
with floats — never display strings.
"""

import pytest
import respx
from httpx import Response

from src.finance.models import ProviderName
from src.finance.providers.genelpara import GenelParaProvider
from tests.helpers import (
    GA_ITEM,
    GENELPARA_URL,
    USD_ITEM,
    XAGUSD_ITEM,
    XAUUSD_ITEM,
)

# ---------------------------------------------------------------------------
# _parse_item: single item -> canonical Quote (pure unit, no HTTP)
# ---------------------------------------------------------------------------


def test_parse_item_strings_to_floats():
    q = GenelParaProvider()._parse_item("USD", USD_ITEM)
    assert q is not None
    assert q.symbol == "USD"
    assert q.buying == pytest.approx(40.5210)
    assert q.selling == pytest.approx(40.5823)
    assert q.source is ProviderName.GENELPARA
    assert q.currency == "TRY" and q.unit == "1 unit"
    # raw string fields survive in extra, untouched
    assert q.extra["yon"] == "moneyUp"
    assert q.extra["kur"] == "TRY"
    assert q.extra["degisim"] == "+0.20"


def test_parse_item_price_kind_fills_price_and_buying():
    q = GenelParaProvider()._parse_item("XAU-ONS", XAUUSD_ITEM)
    assert q is not None
    assert q.price == pytest.approx(2000.00)  # PRICE-kind: price auto-filled
    assert q.buying == pytest.approx(2000.00)
    assert q.currency == "USD" and q.unit == "1 ounce"


def test_parse_item_missing_selling_falls_back_to_buying():
    item = dict(USD_ITEM, satis=None)
    q = GenelParaProvider()._parse_item("USD", item)
    assert q is not None
    assert q.selling == pytest.approx(40.5210)  # satis or alis


def test_parse_item_unparseable_returns_none():
    p = GenelParaProvider()
    assert p._parse_item("USD", {"alis": None}) is None
    assert p._parse_item("USD", {"alis": "abc"}) is None
    assert p._parse_item("USD", {"alis": "", "satis": ""}) is None


# ---------------------------------------------------------------------------
# fetch_quotes: envelope + category grouping + remaining quota tracking
# ---------------------------------------------------------------------------


async def test_fetch_quotes_single_category_and_remaining_tracking():
    payload = {
        "success": True,
        "list": "doviz",
        "count": 1,
        "remaining": 942,
        "data": {"USD": USD_ITEM},
    }
    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(
            return_value=Response(200, json=payload)
        )
        provider = GenelParaProvider()
        quotes = await provider.fetch_quotes({"USD"})

    assert set(quotes) == {"USD"}
    assert quotes["USD"].buying == pytest.approx(40.5210)
    # daily quota tracked on the provider instance
    assert provider.last_remaining == 942
    assert provider.status().consecutive_failures == 0  # record_success ran


async def test_fetch_quotes_groups_categories_and_uses_sembol_all_for_altin():
    requests: list[str] = []

    def handler(request):
        requests.append(request.url.params["sembol"])
        if request.url.params["list"] == "doviz":
            return Response(
                200, json={"success": True, "remaining": 900, "data": {"USD": USD_ITEM}}
            )
        if request.url.params["list"] == "altin":
            return Response(
                200,
                json={
                    "success": True,
                    "remaining": 900,
                    "data": {"XAUUSD": XAUUSD_ITEM, "GA": GA_ITEM},
                },
            )
        if request.url.params["list"] == "emtia":
            return Response(
                200, json={"success": True, "remaining": 900, "data": {"XAGUSD": XAGUSD_ITEM}}
            )
        return Response(500)

    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(side_effect=handler)
        provider = GenelParaProvider()
        quotes = await provider.fetch_quotes({"USD", "XAU-ONS", "XAG-ONS", "XAU-GRAM"})

    assert set(quotes) == {"USD", "XAU-ONS", "XAG-ONS", "XAU-GRAM"}
    assert quotes["XAU-ONS"].price == pytest.approx(2000.00)
    assert quotes["XAG-ONS"].price == pytest.approx(29.75)
    assert quotes["XAU-GRAM"].buying == pytest.approx(2570.00)  # direct GA row
    assert provider.last_remaining == 900
    # altin list is fetched as one "sembol=all" request; doviz per-symbol
    assert "all" in requests
    assert "USD" in requests


async def test_fetch_quotes_bad_envelope_records_failure():
    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(
            return_value=Response(200, json={"success": False, "data": {}})
        )
        provider = GenelParaProvider()
        quotes = await provider.fetch_quotes({"USD"})

    assert quotes == {}
    assert provider.status().consecutive_failures == 1
    assert "envelope" in (provider.status().last_error_msg or "")


async def test_fetch_quotes_http_500_records_failure():
    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(return_value=Response(500))
        provider = GenelParaProvider()
        assert await provider.fetch_quotes({"USD"}) == {}
    assert provider.status().consecutive_failures == 1


async def test_fetch_quotes_unknown_symbols_make_no_request():
    # genelpara_category() -> None for unknown symbols: no HTTP at all.
    provider = GenelParaProvider()
    assert await provider.fetch_quotes({"NOPE"}) == {}
    assert provider.last_remaining is None