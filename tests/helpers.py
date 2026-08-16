"""Shared fixtures/helpers for the finance unit-test suite.

Plain module (not collected by pytest): raw provider envelopes, TCMB XML
fragments, storage mocking and quote builders shared across test files.
"""

from datetime import datetime, timezone

import src.finance.storage as storage
from src.finance import service as service_module
from src.finance.models import ProviderName, Quote

# Provider endpoints (mirrors src/core/config.py defaults; genelpara_base_url
# is rstrip("/")-ed by the provider, so the wire URL has NO trailing slash).
GENELPARA_URL = "https://api.genelpara.com/json"
TCMB_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"

# --- GenelPara API item fixtures (new envelope; every field is a string) ---

USD_ITEM = {
    "alis": "40.5210", "satis": "40.5823", "degisim": "+0.20", "oran": "0.20",
    "yon": "moneyUp", "kur": "TRY", "sembol": "₺",
}
XAUUSD_ITEM = {
    "alis": "2000.00", "satis": "2000.50", "degisim": "+0.35", "oran": "0.35",
    "yon": "moneyUp", "kur": "USD", "sembol": "$",
}
GA_ITEM = {
    "alis": "2570.00", "satis": "2575.00", "degisim": "+0.18", "oran": "0.18",
    "yon": "moneyUp", "kur": "TRY", "sembol": "₺",
}
XAGUSD_ITEM = {
    "alis": "29.7500", "satis": "29.8200", "degisim": "+0.10", "oran": "0.10",
    "yon": "moneyUp", "kur": "USD", "sembol": "$",
}

# --- TCMB today.xml Currency fragments --------------------------------------

USD_CUR = """<Currency Kod="USD" CurrencyCode="USD">
  <Unit>1</Unit><Isim>ABD DOLARI</Isim><CurrencyName>US DOLLAR</CurrencyName>
  <ForexBuying>40.6523</ForexBuying><ForexSelling>40.7148</ForexSelling>
  <BanknoteBuying>40.6171</BanknoteBuying><BanknoteSelling>40.7504</BanknoteSelling>
</Currency>"""
EUR_CUR = """<Currency Kod="EUR" CurrencyCode="EUR">
  <Unit>1</Unit><Isim>AVRO</Isim><CurrencyName>EURO</CurrencyName>
  <ForexBuying>44.5567</ForexBuying><ForexSelling>44.6154</ForexSelling>
  <BanknoteBuying>44.5000</BanknoteBuying><BanknoteSelling>44.7000</BanknoteSelling>
</Currency>"""


def tcmb_xml(tarih: str, currencies: str) -> str:
    """today.xml fixture with a TR-format bulletin date."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Tarih_Date Date="08/14/2026" Tarih="{tarih}">'
        f"{currencies}</Tarih_Date>"
    )


def make_quote(
    symbol: str,
    buying: float | None = None,
    selling: float | None = None,
    price: float | None = None,
    change_pct: float | None = None,
    source: ProviderName = ProviderName.GENELPARA,
    stale: bool = False,
    extra: dict | None = None,
) -> Quote:
    """Quick Quote builder for tests (currency/unit left to caller if needed)."""
    return Quote(
        symbol=symbol,
        buying=buying,
        selling=selling,
        price=price,
        change_pct=change_pct,
        source=source,
        ts=datetime.now(timezone.utc),
        stale=stale,
        extra=extra or {},
    )


class _NoRedis:
    """Redis stand-in returning None — the service's cache-less mode."""

    async def set(self, *args, **kwargs):
        return None

    async def delete(self, *args, **kwargs):
        return None


def mock_storage(monkeypatch, snapshot: dict | None = None):
    """Neutralize DB/Redis persistence so service tests stay fully in-memory.

    ``snapshot`` optionally feeds ``load_latest_db_snapshot`` (the last-resort
    stale fallback path).
    """
    async def _none(*args, **kwargs):
        return None

    async def _false(*args, **kwargs):
        return False

    async def _empty(*args, **kwargs):
        return {}

    async def _snapshot(wanted=None):
        if snapshot is None:
            return {}
        wanted_set = set(wanted) if wanted is not None else set()
        return {s: snapshot[s] for s in wanted_set if s in snapshot}

    monkeypatch.setattr(storage, "get_quotes_cache", _none)
    monkeypatch.setattr(storage, "load_previous_closes", _empty)
    monkeypatch.setattr(storage, "persist_quotes", _false)
    monkeypatch.setattr(storage, "persist_provider_status", _false)
    monkeypatch.setattr(storage, "set_quotes_cache", _none)
    monkeypatch.setattr(storage, "set_stale_marker", _none)
    monkeypatch.setattr(storage, "load_latest_db_snapshot", _snapshot)
    monkeypatch.setattr(service_module, "r", _NoRedis())