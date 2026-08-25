"""FinanceService fallback chain + circuit breaker (fully mocked network).

Chain under test (design spec 2.6): USD -> GENELPARA -> TCMB -> FRANKFURTER
-> YFINANCE_FX (stubbed) -> DB snapshot (stale=True, service-level last
resort). All persistence/Redis is neutralized via ``helpers.mock_storage``;
yfinance is stubbed by the conftest autouse fixture.
"""

import time
from datetime import datetime, timezone

import pytest
import respx
from httpx import Response

import src.finance.storage as storage
from src.finance import FinanceService
from src.finance.models import ProviderName, QuoteBundle
from src.finance.providers.genelpara import GenelParaProvider
from src.finance.providers.registry import provider
from tests.helpers import (
    FRANKFURTER_URL,
    GENELPARA_URL,
    TCMB_URL,
    USD_CUR,
    USD_ITEM,
    make_quote,
    mock_storage,
    tcmb_xml,
)

_FRESH_XML = tcmb_xml(datetime.now(timezone.utc).strftime("%d.%m.%Y"), USD_CUR)


async def test_fallback_genelpara_500_goes_to_tcmb(monkeypatch):
    mock_storage(monkeypatch)
    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(return_value=Response(500))
        respx.get(TCMB_URL).mock(return_value=Response(200, text=_FRESH_XML))
        respx.get(FRANKFURTER_URL).mock(return_value=Response(500))
        bundle = await FinanceService().refresh_symbols({"USD"})

    assert bundle.quotes["USD"].source is ProviderName.TCMB
    assert bundle.quotes["USD"].buying == pytest.approx(40.6523)
    assert bundle.remaining is None  # genelpara failed -> no quota surfaced


async def test_fallback_genelpara_and_tcmb_500_goes_to_frankfurter(monkeypatch):
    mock_storage(monkeypatch)
    frankfurter_payload = {
        "base": "TRY",
        "date": "2026-08-14",
        "rates": {"USD": 0.0321},
    }
    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(return_value=Response(500))
        respx.get(TCMB_URL).mock(return_value=Response(500))
        respx.get(FRANKFURTER_URL).mock(
            return_value=Response(200, json=frankfurter_payload)
        )
        bundle = await FinanceService().refresh_symbols({"USD"})

    quote = bundle.quotes["USD"]
    assert quote.source is ProviderName.FRANKFURTER
    # base=TRY: rates are foreign per 1 TRY -> TRY per unit = 1 / rate
    assert quote.buying == pytest.approx(1.0 / 0.0321)


async def test_all_providers_down_serves_db_snapshot_stale(monkeypatch):
    snapshot_quote = make_quote(
        "USD", buying=40.10, selling=40.20,
        source=ProviderName.DB_SNAPSHOT, stale=True,
    )
    mock_storage(monkeypatch, snapshot={"USD": snapshot_quote})
    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(return_value=Response(500))
        respx.get(TCMB_URL).mock(return_value=Response(500))
        respx.get(FRANKFURTER_URL).mock(return_value=Response(500))
        bundle = await FinanceService().get_quotes(["USD"])

    assert bundle.source is ProviderName.DB_SNAPSHOT
    quote = bundle.quotes["USD"]
    assert quote.stale is True
    assert quote.source is ProviderName.DB_SNAPSHOT
    assert quote.buying == pytest.approx(40.10)


async def test_variety_and_commodity_fall_back_to_db_snapshot_when_genelpara_down(monkeypatch):
    """VARIETY_CHAIN / COMMODITY_CHAIN have no live fallback provider — a
    GenelPara outage must degrade to a stale DB snapshot instead of the
    symbol silently vanishing from the bundle."""
    snapshot_quote = make_quote(
        "XAU-CEYREK", buying=8500.0, selling=8600.0,
        source=ProviderName.DB_SNAPSHOT, stale=True,
    )
    commodity_quote = make_quote(
        "COIL-BRENT-USD", buying=82.0, selling=82.0,
        source=ProviderName.DB_SNAPSHOT, stale=True,
    )
    mock_storage(
        monkeypatch,
        snapshot={"XAU-CEYREK": snapshot_quote, "COIL-BRENT-USD": commodity_quote},
    )
    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(return_value=Response(500))
        bundle = await FinanceService().refresh_symbols({"XAU-CEYREK", "COIL-BRENT-USD"})

    assert set(bundle.quotes) == {"XAU-CEYREK", "COIL-BRENT-USD"}
    assert bundle.quotes["XAU-CEYREK"].stale is True
    assert bundle.quotes["XAU-CEYREK"].source is ProviderName.DB_SNAPSHOT
    assert bundle.quotes["XAU-CEYREK"].buying == pytest.approx(8500.0)
    assert bundle.quotes["COIL-BRENT-USD"].source is ProviderName.DB_SNAPSHOT


async def test_circuit_breaker_opens_after_three_failures():
    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(return_value=Response(500))
        p = GenelParaProvider()
        for _ in range(3):
            assert await p.fetch_quotes({"USD"}) == {}

    assert p.is_available is False
    status = p.status()
    assert status.circuit_open is True
    assert status.consecutive_failures == 3
    assert status.last_error_msg is not None


async def test_service_skips_provider_with_open_circuit(monkeypatch):
    mock_storage(monkeypatch)
    genelpara = provider(ProviderName.GENELPARA)
    assert genelpara is not None
    for _ in range(3):
        genelpara.record_failure(ValueError("test: circuit open"))
    assert genelpara.is_available is False

    with respx.mock:
        genelpara_route = respx.get(url__startswith=GENELPARA_URL).mock(
            return_value=Response(
                200, json={"success": True, "remaining": 500, "data": {"USD": USD_ITEM}}
            )
        )
        respx.get(TCMB_URL).mock(return_value=Response(200, text=_FRESH_XML))
        respx.get(FRANKFURTER_URL).mock(return_value=Response(500))
        bundle = await FinanceService().refresh_symbols({"USD"})

    assert genelpara_route.call_count == 0  # never contacted while open
    assert bundle.quotes["USD"].source is ProviderName.TCMB


async def test_circuit_half_open_probe_and_success_resets():
    p = GenelParaProvider()
    for _ in range(3):
        p.record_failure(ValueError("test"))
    assert p.is_available is False
    assert p._circuit.consecutive_failures == 3

    # cooldown elapses -> half-open: probes allowed again
    p._circuit.open_until = time.monotonic() - 1.0
    assert p.is_available is True

    with respx.mock:
        respx.get(url__startswith=GENELPARA_URL).mock(
            return_value=Response(
                200, json={"success": True, "remaining": 700, "data": {"USD": USD_ITEM}}
            )
        )
        quotes = await p.fetch_quotes({"USD"})

    assert set(quotes) == {"USD"}
    status = p.status()  # breaker fully reset; last_error stays as history
    assert status.consecutive_failures == 0
    assert status.circuit_open is False
    assert status.last_error_msg is None


async def test_cached_bundle_hits_when_a_symbol_is_never_delivered(monkeypatch):
    """Regression: JOD (never delivered by any provider) must not defeat the cache."""
    mock_storage(monkeypatch)
    cached = QuoteBundle(
        ts=datetime.now(timezone.utc),
        source=ProviderName.GENELPARA,
        quotes={
            "USD": make_quote("USD", buying=40.5, selling=40.6),
            "EUR": make_quote("EUR", buying=44.5, selling=44.6),
            "GBP": make_quote("GBP", buying=52.0, selling=52.1),
        },
    )

    async def _cached():
        return cached

    refresh_calls = {"count": 0}

    async def _no_refresh(*args, **kwargs):
        refresh_calls["count"] += 1

    monkeypatch.setattr(storage, "get_quotes_cache", _cached)
    monkeypatch.setattr(FinanceService, "refresh_all", _no_refresh)

    bundle = await FinanceService().get_quotes(["USD", "EUR", "GBP", "JOD"])

    assert refresh_calls["count"] == 0  # served from the cache, no refresh
    assert set(bundle.quotes) == {"USD", "EUR", "GBP"}
    assert "JOD" not in bundle.quotes


async def test_cached_bundle_misses_when_no_symbol_is_available(monkeypatch):
    mock_storage(monkeypatch)
    cached = QuoteBundle(
        ts=datetime.now(timezone.utc),
        source=ProviderName.GENELPARA,
        quotes={
            "USD": make_quote("USD", buying=40.5, selling=40.6),
            "EUR": make_quote("EUR", buying=44.5, selling=44.6),
        },
    )

    async def _cached():
        return cached

    refresh_calls = {"count": 0}

    async def _refresh(*args, **kwargs):
        refresh_calls["count"] += 1
        return QuoteBundle(ts=datetime.now(timezone.utc), quotes={})

    monkeypatch.setattr(storage, "get_quotes_cache", _cached)
    monkeypatch.setattr(FinanceService, "refresh_all", _refresh)

    bundle = await FinanceService().get_quotes(["JOD"])

    assert refresh_calls["count"] == 1  # cache unusable -> refresh attempted
    assert bundle.quotes == {}
