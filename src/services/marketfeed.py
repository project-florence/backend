"""Market feed orkestrasyonu: haber + endeks + makro takvim (Redis cache).

``src/clients/feeds.py``'deki fetch+normalize fonksiyonlarini cagirir ve
sonuclari Redis'te onbellekler (down-tolerant — Redis yoksa dogrudan fetch
eder, asla cokmez). Gelecekteki cron/digest isi bu servisin ``get_*``
fonksiyonlarini cagirir.

Cache anahtarlari:
- ``marketfeed:news``    — haber listesi (TTL ``news_cache_ttl``, 30 dk)
- ``marketfeed:indices`` — endeks listesi (TTL ``index_cache_ttl``, 25 dk)
- ``marketfeed:events``  — makro takvim   (TTL ``events_cache_ttl``, 8 sa)
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List

from pydantic import TypeAdapter

from src.clients.feeds import (
    FeedItem,
    IndexQuote,
    MacroEvent,
    fetch_indices,
    fetch_macro_calendar,
    fetch_rss,
)
from src.core.config import get_config
from src.core.redis import r

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Yardimcilar
# --------------------------------------------------------------------------
def _decode_list(model, raw: str | None) -> list | None:
    if not raw:
        return None
    try:
        adapter = TypeAdapter(List[model])  # type: ignore[valid-type]
        parsed = adapter.validate_json(raw)
        return parsed if isinstance(parsed, list) else None
    except Exception:
        return None


def _encode_list(items: list) -> str:
    return json.dumps([it.model_dump(mode="json") for it in items], ensure_ascii=False)


# --------------------------------------------------------------------------
# 1. Haber beslemesi
# --------------------------------------------------------------------------
async def _fetch_all_news(cfg: dict) -> list[FeedItem]:
    """Config'teki tum RSS kaynaklarini Semaphore ile paralel ceker."""
    urls = cfg.get("rss_urls") or {}
    if not urls:
        return []
    sem = asyncio.Semaphore(int(cfg.get("rss_semaphore", 5)))
    limit = int(cfg.get("news_limit", 20))

    async def one(source: str, url: str) -> list[FeedItem]:
        async with sem:
            return await fetch_rss(url, limit=limit, source=source)

    results = await asyncio.gather(
        *(one(src, u) for src, u in urls.items()), return_exceptions=True
    )
    items: list[FeedItem] = []
    for res in results:
        if isinstance(res, Exception):
            logger.warning("news source failed: %s", res)
            continue
        items.extend(res)
    return items


async def get_news_feed(limit: int | None = None) -> list[FeedItem]:
    """Cache-first haber listesi, kaynaklar birlestirilir ve tarihe gore siralanir."""
    cfg = get_config()["marketfeed"]
    limit = limit or int(cfg.get("news_limit", 20))

    cached = _decode_list(FeedItem, await r.get("marketfeed:news"))
    if cached is not None:
        return cached[:limit]

    items = await _fetch_all_news(cfg)
    # published'a gore azalan sirala; None'lar en sona
    items.sort(
        key=lambda it: it.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    items = items[:limit]

    encoded = _encode_list(items)
    await r.set("marketfeed:news", encoded, ex=int(cfg.get("news_cache_ttl", 1800)))
    return items


# --------------------------------------------------------------------------
# 2. Global endeksler
# --------------------------------------------------------------------------
async def get_indices() -> list[IndexQuote]:
    """Cache-first global endeks cotasyonlari (yerel sembol toleransli)."""
    cfg = get_config()["marketfeed"]

    cached = _decode_list(IndexQuote, await r.get("marketfeed:indices"))
    if cached is not None:
        return cached

    quotes = await fetch_indices(cfg.get("index_symbols"), cfg.get("index_names"))

    await r.set(
        "marketfeed:indices",
        _encode_list(quotes),
        ex=int(cfg.get("index_cache_ttl", 1500)),
    )
    return quotes


# --------------------------------------------------------------------------
# 3. Makro takvim
# --------------------------------------------------------------------------
async def get_macro_events(days: int | None = None) -> list[MacroEvent]:
    """Cache-first yaklasan makro yayin tarihleri (FRED_API_KEY yoksa [])."""
    cfg = get_config()["marketfeed"]
    days = days or int(cfg.get("events_days", 45))

    cached = _decode_list(MacroEvent, await r.get("marketfeed:events"))
    if cached is not None:
        return cached

    events = await fetch_macro_calendar(days)

    await r.set(
        "marketfeed:events",
        _encode_list(events),
        ex=int(cfg.get("events_cache_ttl", 28800)),
    )
    return events
