"""Frankfurter (ECB reference rates) provider — third FX fallback.

Domain is ``api.frankfurter.dev`` (the legacy ``api.frankfurter.app`` returns
301). We query with ``base=TRY``; the response rates are *foreign units per
1 TRY*, so the TRY-per-unit quote is the inverse: ``1 / rates[code]``.
Daily values (published ~16:00 CET) — weekend data is expected and accepted.
"""

import logging
from datetime import datetime, timezone

from src.clients.http import get_client
from src.core.config import get_config
from src.finance.models import ProviderName, Quote
from src.finance.providers.base import BaseProvider, make_quote, safe_float
from src.finance.symbols import SYMBOL_REGISTRY

logger = logging.getLogger(__name__)


class FrankfurterProvider(BaseProvider):
    """ECB reference cross-rates against TRY."""

    name = ProviderName.FRANKFURTER

    async def fetch_quotes(self, symbols: set[str]) -> dict[str, Quote]:
        wanted = {s for s in symbols if s in self.provides}
        if not wanted:
            return {}
        base = get_config()["finance"]["frankfurter_url"]
        try:
            client = await get_client()
            resp = await client.get(
                base,
                params={"base": "TRY"},
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            self.record_failure(exc)
            return {}
        if not isinstance(payload, dict) or not isinstance(payload.get("rates"), dict):
            self.record_failure(ValueError("frankfurter: missing rates"))
            return {}

        rates = payload["rates"]
        ref_date = payload.get("date")
        ts = datetime.now(timezone.utc)
        if isinstance(ref_date, str):
            try:
                ts = datetime.fromisoformat(ref_date).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        results: dict[str, Quote] = {}
        for symbol in sorted(wanted):
            code = SYMBOL_REGISTRY[symbol].provider_symbols[self.name]
            rate = safe_float(rates.get(code))
            if rate is None or rate <= 0:
                continue
            # base=TRY: rates[code] = foreign per 1 TRY -> TRY per unit = 1/rate.
            value = 1.0 / rate
            results[symbol] = make_quote(
                symbol,
                buying=value,
                selling=value,
                ts=ts,
                source=self.name,
                extra={"ecb_date": ref_date, "base": payload.get("base")},
            )
        if results:
            self.record_success()
        else:
            self.record_failure(
                ValueError(f"frankfurter: none of the requested symbols in rates")
            )
        return results