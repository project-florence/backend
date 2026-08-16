"""TCMB today.xml parsing — values + weekend/stale tolerance.

The bulletin is published on business days ~15:30 TRT; an aged bulletin
(``Tarih`` older than ``fallback_stale_max_days``) must be treated as
unavailable (empty result) WITHOUT tripping the circuit breaker — it is a
time-based condition, not a crash.
"""

from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response

from src.finance.models import ProviderName
from src.finance.providers.base import BaseProvider
from src.finance.providers.registry import provider
from src.finance.providers.tcmb import TcmbProvider
from tests.helpers import EUR_CUR, TCMB_URL, USD_CUR, tcmb_xml


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%d.%m.%Y")


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d.%m.%Y")


# ---------------------------------------------------------------------------
# _parse_xml (pure unit — no HTTP)
# ---------------------------------------------------------------------------


def test_parse_xml_values_and_banknote_extras():
    quotes, stale = TcmbProvider._parse_xml(
        tcmb_xml(_today(), USD_CUR + EUR_CUR), {"USD", "EUR"}, 3
    )
    assert stale is False
    assert set(quotes) == {"USD", "EUR"}
    assert quotes["USD"].buying == pytest.approx(40.6523)
    assert quotes["USD"].selling == pytest.approx(40.7148)
    assert quotes["USD"].source is ProviderName.TCMB
    assert quotes["USD"].extra["banknote_buying"] == pytest.approx(40.6171)
    assert quotes["USD"].extra["banknote_selling"] == pytest.approx(40.7504)
    assert quotes["USD"].extra["unit"] == "1"
    assert quotes["EUR"].buying == pytest.approx(44.5567)
    assert quotes["EUR"].selling == pytest.approx(44.6154)


def test_parse_xml_stale_bulletin_yields_empty_and_stale_flag():
    quotes, stale = TcmbProvider._parse_xml(
        tcmb_xml(_days_ago(10), USD_CUR), {"USD"}, 3
    )
    assert quotes == {}
    assert stale is True


def test_parse_xml_fresh_bulletin_within_tolerance():
    # 2 days old, tolerance 3 -> still servable (weekend tolerance)
    quotes, stale = TcmbProvider._parse_xml(
        tcmb_xml(_days_ago(2), USD_CUR), {"USD"}, 3
    )
    assert set(quotes) == {"USD"}
    assert stale is False


def test_parse_xml_drops_unknown_and_unrequested_currencies():
    unknown = (
        '<Currency Kod="XYZ"><Unit>1</Unit><Isim>X</Isim>'
        "<ForexBuying>5</ForexBuying><ForexSelling>6</ForexSelling></Currency>"
    )
    quotes, stale = TcmbProvider._parse_xml(
        tcmb_xml(_today(), USD_CUR + EUR_CUR + unknown), {"USD"}, 3
    )
    assert stale is False
    assert set(quotes) == {"USD"}


def test_parse_xml_skips_currencies_without_rates():
    empty = (
        '<Currency Kod="USD"><Unit>1</Unit><Isim>X</Isim>'
        "<ForexBuying/><ForexSelling/></Currency>"
    )
    quotes, stale = TcmbProvider._parse_xml(tcmb_xml(_today(), empty), {"USD"}, 3)
    assert quotes == {}
    assert stale is False


# ---------------------------------------------------------------------------
# fetch_quotes (respx-mocked HTTP)
#
# NOTE: fetch-level tests use the registry's shared instance — ``provides``
# is only populated by the registry builder, so a bare ``TcmbProvider()`` has
# an empty ``provides`` set and skips every request. The conftest autouse
# fixture resets the shared circuits between tests.
# ---------------------------------------------------------------------------


def _shared() -> BaseProvider:
    p = provider(ProviderName.TCMB)
    assert p is not None
    return p


async def test_fetch_quotes_success():
    with respx.mock:
        respx.get(TCMB_URL).mock(return_value=Response(200, text=tcmb_xml(_today(), USD_CUR)))
        quotes = await _shared().fetch_quotes({"USD", "EUR"})

    assert set(quotes) == {"USD"}  # EUR row absent from this bulletin
    assert quotes["USD"].buying == pytest.approx(40.6523)
    assert _shared().status().consecutive_failures == 0  # success reset


async def test_fetch_quotes_stale_skips_without_circuit_trip():
    with respx.mock:
        respx.get(TCMB_URL).mock(
            return_value=Response(200, text=tcmb_xml(_days_ago(10), USD_CUR))
        )
        quotes = await _shared().fetch_quotes({"USD"})
    assert quotes == {}
    # time-based skip: no failure recorded, circuit untouched
    assert _shared().status().consecutive_failures == 0
    assert _shared().status().circuit_open is False


async def test_fetch_quotes_http_error_records_failure():
    with respx.mock:
        respx.get(TCMB_URL).mock(return_value=Response(500))
        quotes = await _shared().fetch_quotes({"USD"})
    assert quotes == {}
    assert _shared().status().consecutive_failures == 1
    assert _shared().status().last_error_msg is not None


async def test_fetch_quotes_no_requested_currencies_records_failure():
    with respx.mock:
        # Bulletin without USD while USD was requested
        respx.get(TCMB_URL).mock(
            return_value=Response(200, text=tcmb_xml(_today(), EUR_CUR))
        )
        quotes = await _shared().fetch_quotes({"USD"})
    assert quotes == {}
    assert _shared().status().consecutive_failures == 1
    assert "no requested currencies" in (_shared().status().last_error_msg or "")