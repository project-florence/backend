import asyncio
import json

import pykap

from src.core.config import get_config
from src.core.database import db
from src.core.redis import r


def _cache_interval():
    return get_config()["get_bist_companies"]["cache_interval"]


async def _fetch_and_persist_all():
    tickers, companies = await asyncio.to_thread(
        lambda: (pykap.bist_company_list(), pykap.get_bist_companies(output_format="dict"))
    )

    company_map = {c["ticker"]: c["name"] for c in companies}
    async with db.cursor(row_factory=None) as cur:
        for code in tickers:
            name = company_map.get(code)
            await cur.execute(
                "INSERT INTO tickers (code, name, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, updated_at = NOW()",
                (code, name),
            )
        for c in companies:
            await cur.execute(
                "INSERT INTO companies (ticker, name, summary_page, city, auditor, company_id, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, NOW()) "
                "ON CONFLICT (ticker) DO UPDATE SET "
                "name = EXCLUDED.name, summary_page = EXCLUDED.summary_page, "
                "city = EXCLUDED.city, auditor = EXCLUDED.auditor, "
                "company_id = EXCLUDED.company_id, updated_at = NOW()",
                (c["ticker"], c["name"], c["summary_page"], c["city"], c["auditor"], c["company_id"]),
            )
    await db.commit()

    tickers_json = json.dumps(tickers)
    companies_json = json.dumps(companies)
    await r.set("tickers", tickers_json, ex=_cache_interval())
    await r.set("companies", companies_json, ex=_cache_interval())
    return tickers_json, companies_json


async def _db_get_tickers():
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT code FROM tickers ORDER BY code")
        rows = await cur.fetchall()
        if not rows:
            return None
        return json.dumps([row[0] for row in rows])


async def _db_get_companies():
    async with db.cursor() as cur:
        await cur.execute("SELECT * FROM companies ORDER BY ticker")
        rows = await cur.fetchall()
        if not rows:
            return None
        return json.dumps([dict(row) for row in rows], default=str)


async def get_bist_tickers_as_list():
    return await asyncio.to_thread(pykap.bist_company_list)


async def get_bist_tickers_as_json():
    return json.dumps(await asyncio.to_thread(pykap.bist_company_list))


async def get_bist_tickers_as_json_from_redis():
    tickers = await r.get("tickers")
    if tickers is not None:
        return tickers

    tickers = await _db_get_tickers()
    if tickers is not None:
        await r.set("tickers", tickers, ex=_cache_interval())
        return tickers

    tickers_json, _ = await _fetch_and_persist_all()
    return tickers_json


async def get_bist_tickers_as_dict_from_redis():
    return json.loads(await get_bist_tickers_as_json_from_redis())


async def get_bist_companies_as_dict():
    return await asyncio.to_thread(pykap.get_bist_companies, output_format="dict")


async def get_bist_companies_as_json():
    return await asyncio.to_thread(pykap.get_bist_companies, output_format="json")


async def get_bist_companies_as_json_from_redis():
    companies = await r.get("companies")
    if companies is not None:
        return companies

    companies = await _db_get_companies()
    if companies is not None:
        await r.set("companies", companies, ex=_cache_interval())
        return companies

    _, companies_json = await _fetch_and_persist_all()
    return companies_json


async def get_bist_companies_as_dict_from_redis():
    return json.loads(await get_bist_companies_as_json_from_redis())


async def cache_tickers_and_companies():
    await get_bist_companies_as_json_from_redis()
    await get_bist_tickers_as_json_from_redis()


async def sync_tickers_and_companies():
    await _fetch_and_persist_all()


async def search_companies_by_text(text, limit: int = 20):
    from src.services.search import search_companies as _search
    return await _search(text, limit=limit)


async def is_valid_bist_ticker(ticker: str) -> bool:
    ticker = ticker.upper()
    return ticker in await get_bist_tickers_as_dict_from_redis()
