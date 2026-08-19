"""Async harness tools for the market digest.

Each tool is an ``async`` function that wraps an existing backend service or
client and is down-tolerant: on any external failure it returns an
"unavailable" marker / empty container and never raises. Imports happen inside
the function body to avoid circular imports at module load time.
"""

from datetime import datetime, timezone

_ARTICLE_TEXT_MAX = 4000
_ECONOMY_SYMBOLS = ("USD", "EUR", "GBP", "XAU-GRAM", "XAU-ONS")


async def get_datetime() -> dict:
    """Current UTC timestamp as ISO-8601 string."""
    return {"now": datetime.now(timezone.utc).isoformat()}


async def get_market_status() -> dict:
    """BIST market status payload (open/closed, next open, holiday)."""
    try:
        from src.services.market import get_market_status_payload

        return await get_market_status_payload()
    except Exception:
        return {"unavailable": True}


async def get_market_snapshot() -> dict:
    """Compact today-market snapshot: one call gathers market, indices, rates,
    gainers/losers, IPOs and macro events. Every field is down-tolerant to
    empty and this tool never raises."""
    import asyncio

    market, indices, rates, gainers_losers, ipos, macro_events = await asyncio.gather(
        get_market_status(),
        get_indices(),
        get_economy_quotes(),
        get_gainers_losers(),
        get_ipos(),
        get_macro_events(),
        return_exceptions=True,
    )
    return {
        "market": market if not isinstance(market, Exception) else {},
        "indices": indices if not isinstance(indices, Exception) else [],
        "rates": rates if not isinstance(rates, Exception) else {},
        "gainers_losers": (
            gainers_losers
            if not isinstance(gainers_losers, Exception)
            else {"gainers": [], "losers": []}
        ),
        "ipos": ipos if not isinstance(ipos, Exception) else [],
        "macro_events": macro_events if not isinstance(macro_events, Exception) else [],
    }


async def get_news_feed(limit: int | None = None) -> list:
    """Top news from the combined RSS market feed."""
    try:
        from src.services.marketfeed import get_news_feed as _get_news_feed

        items = await _get_news_feed(limit=limit)
        return [
            {
                "source": it.source,
                "title": it.title,
                "link": it.link,
                "published": it.published.isoformat() if it.published else None,
            }
            for it in items
        ]
    except Exception:
        return []


async def get_indices() -> list:
    """Global index quotes (S&P 500, BIST 100, ...)."""
    try:
        from src.services.marketfeed import get_indices as _get_indices

        items = await _get_indices()
        return [
            {
                "key": q.key,
                "name": q.name,
                "value": q.value,
                "change_pct": q.change_pct,
                "as_of": q.as_of.isoformat() if q.as_of else None,
            }
            for q in items
        ]
    except Exception:
        return []


async def get_macro_events() -> list:
    """Upcoming macro calendar events (FRED releases/dates)."""
    try:
        from src.services.marketfeed import get_macro_events as _get_macro_events

        items = await _get_macro_events()
        return [
            {
                "date": e.date.isoformat() if e.date else None,
                "region": e.region,
                "event": e.event,
                "series": e.series,
                "impact": e.impact,
            }
            for e in items
        ]
    except Exception:
        return []


async def get_economy_quotes() -> dict:
    """Compact FX/precious-metal quotes for the digest's macro section."""
    try:
        from src.finance import finance_service

        bundle = await finance_service.get_quotes(list(_ECONOMY_SYMBOLS))
        out: dict = {}
        for symbol in _ECONOMY_SYMBOLS:
            quote = bundle.quotes.get(symbol)
            if quote is None:
                continue
            last = quote.price if quote.price is not None else quote.buying
            out[symbol] = {"last": last, "change_pct": quote.change_pct}
        return out
    except Exception:
        return {}


async def get_macroeconomy() -> dict:
    """FRED macro data (GDP, rates, CPI, ...). Empty dict when unavailable."""
    try:
        from src.clients.macroeconomy import get_macroeconomy_data

        data = await get_macroeconomy_data()
        if data is None:
            return {}
        return data.model_dump()
    except Exception:
        return {}


async def get_gainers_losers() -> dict:
    """Top 5 BIST gainers and losers by daily change percent."""
    try:
        from src.services.company import get_companies_summary

        def _compact(rows: list) -> list:
            return [
                {
                    "ticker": row["ticker"],
                    "last_price": row["last_price"],
                    "change_pct": row["change_pct"],
                }
                for row in rows
            ]

        gainers = await get_companies_summary(sort="gainers", limit=5)
        losers = await get_companies_summary(sort="losers", limit=5)
        return {
            "gainers": _compact(gainers.get("data", [])),
            "losers": _compact(losers.get("data", [])),
        }
    except Exception:
        return {"gainers": [], "losers": []}


async def get_ipos() -> list:
    """Compact list of upcoming and active IPOs (deduplicated by slug)."""
    try:
        import json

        from src.services.ipo import get_active_ipos, get_upcoming_ipos

        upcoming = json.loads(await get_upcoming_ipos())
        active = json.loads(await get_active_ipos())
        seen: set = set()
        out: list = []
        for item in list(upcoming or []) + list(active or []):
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            out.append(
                {
                    "slug": slug,
                    "title": item.get("title"),
                    "link": item.get("link"),
                    "date": item.get("date"),
                }
            )
        return out
    except Exception:
        return []


async def search_news(query: str, limit: int = 10) -> list:
    """SearXNG news search results as {title, link} dicts."""
    try:
        from src.clients.search import news_search

        items = await news_search(query, limit=limit)
        return [{"title": it.title, "link": it.url} for it in items]
    except Exception:
        return []


async def fetch_article_text(url: str) -> str:
    """Full-text extraction of a news article (trafilatura), truncated."""
    try:
        import asyncio

        from src.clients.scraping import get_text_from_url

        text = await asyncio.to_thread(get_text_from_url, url)
        return (text or "")[:_ARTICLE_TEXT_MAX]
    except Exception:
        return ""
