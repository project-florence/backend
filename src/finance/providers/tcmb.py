"""TCMB official FX bulletin provider — key-less backup for FX spot.

Parses ``https://www.tcmb.gov.tr/kurlar/today.xml`` (ElementTree inside
``asyncio.to_thread`` per AGENTS.md). The bulletin is published on business
days around 15:30 TRT; on weekends/holidays the latest bulletin ages. The
``Tarih`` attribute is checked against ``finance.fallback_stale_max_days`` —
an old bulletin is treated as *unavailable* (empty result, no circuit trip,
it is a time-based condition, not a crash) so the chain moves to the next
source.
"""

import asyncio
import logging
from datetime import datetime, timezone
from xml.etree import ElementTree

from src.clients.http import get_client
from src.core.config import get_config
from src.finance.models import ProviderName, Quote
from src.finance.providers.base import BaseProvider, make_quote, safe_float

logger = logging.getLogger(__name__)


class TcmbProvider(BaseProvider):
    """Official TCMB bulletin: ForexBuying/ForexSelling as bid/ask, banknotes in extra."""

    name = ProviderName.TCMB

    async def fetch_quotes(self, symbols: set[str]) -> dict[str, Quote]:
        wanted = {s for s in symbols if s in self.provides}
        if not wanted:
            return {}
        cfg = get_config()["finance"]
        try:
            client = await get_client()
            resp = await client.get(
                cfg["tcmb_url"],
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            quotes, stale = await asyncio.to_thread(
                self._parse_xml, resp.text, wanted, int(cfg["fallback_stale_max_days"])
            )
        except Exception as exc:
            self.record_failure(exc)
            return {}
        if stale:
            logger.warning(
                "TCMB bulletin older than %s days — skipping (weekend tolerance)",
                cfg["fallback_stale_max_days"],
            )
            return {}
        if not quotes:
            self.record_failure(ValueError("tcmb: no requested currencies in today.xml"))
            return {}
        self.record_success()
        return quotes

    @staticmethod
    def _parse_xml(text: str, wanted: set[str], max_days: int) -> tuple[dict[str, Quote], bool]:
        """Sync XML parse (runs in a worker thread). Returns (quotes, stale)."""
        root = ElementTree.fromstring(text)

        # Bulletin date: "Tarih"="14.08.2026" (TR) or "Date"="08/14/2026" (US).
        bulletin_date: datetime | None = None
        raw_date = root.attrib.get("Tarih") or root.attrib.get("Date")
        if raw_date:
            for fmt in ("%d.%m.%Y", "%m/%d/%Y"):
                try:
                    bulletin_date = datetime.strptime(raw_date.strip(), fmt).replace(
                        tzinfo=timezone.utc
                    )
                    break
                except ValueError:
                    continue
        if bulletin_date is not None:
            age_days = (datetime.now(timezone.utc) - bulletin_date).days
            if age_days > max_days:
                return {}, True

        quotes: dict[str, Quote] = {}
        for currency in root.iter("Currency"):
            code = currency.attrib.get("Kod")
            if code not in wanted:
                continue
            buying = safe_float(currency.findtext("ForexBuying"))
            selling = safe_float(currency.findtext("ForexSelling"))
            if buying is None and selling is None:
                continue
            if selling is None:
                selling = buying
            quotes[code] = make_quote(
                code,
                buying=buying,
                selling=selling,
                ts=bulletin_date or datetime.now(timezone.utc),
                source=ProviderName.TCMB,
                extra={
                    "unit": currency.findtext("Unit"),
                    "name": currency.findtext("Isim"),
                    "banknote_buying": safe_float(currency.findtext("BanknoteBuying")),
                    "banknote_selling": safe_float(currency.findtext("BanknoteSelling")),
                },
            )
        return quotes, False