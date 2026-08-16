"""GenelPara provider — new API contract (verified live 2026-08-16).

Endpoint: ``{base}?list=doviz|altin|emtia&sembol=...``
Envelope: ``{"success": true, "list": ..., "count": N, "remaining": N, "data": {...}}``
Item: ``{"alis": "47.8424", "satis": "47.9149", "degisim": "+0.20", "oran": ...,
"yon": "moneyUp", "kur": "TRY", "sembol": "₺"}`` — every field is a string.

The daily quota (``remaining``, 1000/day per IP) is tracked on the provider
and surfaced through the bundle. The API's own ``degisim``/``oran`` fields are
kept in ``extra`` but never trusted for ``change_pct`` (weekend zeros).
"""

import logging
from datetime import datetime, timezone

from src.clients.http import get_client
from src.core.config import get_config
from src.finance.models import ProviderName, Quote
from src.finance.providers.base import BaseProvider, make_quote, safe_float
from src.finance.symbols import SYMBOL_REGISTRY, genelpara_category

logger = logging.getLogger(__name__)

_LOW_QUOTA_WARN = 100


class GenelParaProvider(BaseProvider):
    """Primary source: FX spot, all Turkish gold varieties, ounce metals."""

    name = ProviderName.GENELPARA

    def __init__(self) -> None:
        super().__init__()
        self.last_remaining: int | None = None

    async def fetch_quotes(self, symbols: set[str]) -> dict[str, Quote]:
        # Group the requested canonical symbols by GenelPara category list.
        per_category: dict[str, set[str]] = {}
        for symbol in symbols:
            category = genelpara_category(symbol)
            if category is not None:
                per_category.setdefault(category, set()).add(symbol)
        if not per_category:
            return {}

        base = get_config()["finance"]["genelpara_base_url"].rstrip("/")
        client = await get_client()
        results: dict[str, Quote] = {}
        errors: list[Exception] = []
        fetched_any = False

        for category, syms in per_category.items():
            raw_symbols = sorted(
                SYMBOL_REGISTRY[s].provider_symbols[self.name] for s in syms
            )
            params = {"list": category}
            if category == "altin":
                # One request covers the whole gold list (18 varieties).
                params["sembol"] = "all"
            else:
                params["sembol"] = ",".join(raw_symbols)
            try:
                resp = await client.get(
                    base,
                    params=params,
                    timeout=15,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                errors.append(exc)
                logger.warning("GenelPara %s request failed: %s", category, exc)
                continue
            fetched_any = True

            if not isinstance(payload, dict) or payload.get("success") is not True:
                errors.append(ValueError(f"GenelPara {category}: bad envelope"))
                continue
            remaining = payload.get("remaining")
            if isinstance(remaining, (int, float)):
                self.last_remaining = int(remaining)
                if remaining <= _LOW_QUOTA_WARN:
                    logger.warning("GenelPara daily quota low: %s remaining", remaining)
            data = payload.get("data")
            if not isinstance(data, dict):
                errors.append(ValueError(f"GenelPara {category}: missing data"))
                continue
            for symbol in sorted(syms):
                raw = SYMBOL_REGISTRY[symbol].provider_symbols[self.name]
                item = data.get(raw)
                if not isinstance(item, dict):
                    continue
                quote = self._parse_item(symbol, item)
                if quote is not None:
                    results[symbol] = quote

        if results:
            self.record_success()
        elif errors or not fetched_any:
            if errors:
                self.record_failure(errors[0])
            else:
                self.record_failure(ValueError("GenelPara: no symbols requested served"))

        if self.last_remaining is not None:
            logger.info(
                "GenelPara refresh: %d quotes from %d categories, quota remaining=%s",
                len(results), len(per_category), self.last_remaining,
            )
        return results

    def _parse_item(self, symbol: str, item: dict) -> Quote | None:
        """Single GenelPara item -> canonical Quote (string fields -> floats)."""
        d = SYMBOL_REGISTRY[symbol]
        alis = safe_float(item.get("alis"))
        if alis is None:
            return None
        satis = safe_float(item.get("satis")) or alis
        extra = {
            "degisim": item.get("degisim"),
            "oran": item.get("oran"),
            "yon": item.get("yon"),
            "kur": item.get("kur"),
            "sembol": item.get("sembol"),
        }
        ts = datetime.now(timezone.utc)
        # make_quote fills `price` automatically for PRICE-kind symbols.
        return make_quote(
            symbol,
            buying=alis,
            selling=satis,
            ts=ts,
            source=self.name,
            extra=extra,
        )