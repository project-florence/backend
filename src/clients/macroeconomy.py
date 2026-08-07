import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fredapi import Fred
from pydantic import BaseModel

from src.core.config import get_config
from src.core.database import db
from src.core.redis import r


class MacroeconomyData(BaseModel):
    usa_gdp: float
    usa_real_gdp: float
    fed_funds: float
    fed_funds_rate: float
    usa_unrate: float
    brent_crude_oil_price: float
    wti_crude_oil_price: float
    usa_consumer_cpi: float
    usa_10y_treasury: float
    dxy: float
    vix: float
    sp500: float
    nasdaq: float
    bitcoin: float


_MACRO_FIELDS = (
    "usa_gdp", "usa_real_gdp", "fed_funds", "fed_funds_rate", "usa_unrate",
    "brent_crude_oil_price", "wti_crude_oil_price", "usa_consumer_cpi",
    "usa_10y_treasury", "dxy", "vix", "sp500", "nasdaq", "bitcoin",
)


load_dotenv()

fred_api_key = os.getenv("FRED_API_KEY")
if not fred_api_key:
    raise ValueError("FRED_API_KEY not set")

fred = Fred(api_key=fred_api_key)


def _get_latest_fred_val(series_id: str) -> float:
    series = fred.get_series(series_id).dropna()
    return float(series.iloc[-1])


def _fetch_macroeconomy_data() -> MacroeconomyData:
    series_ids = {
        "usa_gdp": "GDP",
        "usa_real_gdp": "GDPC1",
        "fed_funds": "FEDFUNDS",
        "fed_funds_rate": "DFF",
        "usa_unrate": "UNRATE",
        "brent_crude_oil_price": "DCOILBRENTEU",
        "wti_crude_oil_price": "DCOILWTICO",
        "usa_consumer_cpi": "CPIAUCSL",
        "usa_10y_treasury": "DGS10",
        "dxy": "DTWEXBGS",
        "vix": "VIXCLS",
        "sp500": "SP500",
        "nasdaq": "NASDAQCOM",
        "bitcoin": "CBBTCUSD",
    }
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = dict(zip(series_ids, executor.map(_get_latest_fred_val, series_ids.values())))
    return MacroeconomyData(**values)


async def _load_recent_database_snapshot() -> MacroeconomyData | None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=get_config()["macroeconomy"]["cache_ttl"])
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            """
            SELECT usa_gdp, usa_real_gdp, fed_funds, fed_funds_rate, usa_unrate,
                   brent_crude_oil_price, wti_crude_oil_price, usa_consumer_cpi,
                   usa_10y_treasury, dxy, vix, sp500, nasdaq, bitcoin, timestamp
            FROM macroeconomy
            ORDER BY timestamp DESC
            LIMIT 1
            """
        )
        row = await cur.fetchone()
    if not row or row[-1] is None:
        return None
    timestamp = row[-1]
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    if timestamp < cutoff:
        return None
    return MacroeconomyData(**dict(zip(_MACRO_FIELDS, row[:-1])))


async def _cache_and_persist_macroeconomy_data(mdata: MacroeconomyData) -> None:
    await r.set("MacroeconomyData", mdata.model_dump_json(), ex=get_config()["macroeconomy"]["cache_ttl"])

    async with db.cursor() as cur:
        await cur.execute("""
        INSERT INTO macroeconomy
            (usa_gdp, usa_real_gdp, fed_funds, fed_funds_rate, usa_unrate,
             brent_crude_oil_price, wti_crude_oil_price, usa_consumer_cpi,
             usa_10y_treasury, dxy, vix, sp500, nasdaq, bitcoin)
        VALUES (
            %(usa_gdp)s, %(usa_real_gdp)s, %(fed_funds)s, %(fed_funds_rate)s, %(usa_unrate)s,
            %(brent_crude_oil_price)s, %(wti_crude_oil_price)s, %(usa_consumer_cpi)s,
            %(usa_10y_treasury)s, %(dxy)s, %(vix)s, %(sp500)s, %(nasdaq)s, %(bitcoin)s
        );
        """, mdata.model_dump())
        await db.commit()


async def get_macroeconomy_data() -> MacroeconomyData:
    mdata = await r.get("MacroeconomyData")
    if mdata:
        return MacroeconomyData.model_validate_json(mdata)

    snapshot = await _load_recent_database_snapshot()
    if snapshot:
        await r.set("MacroeconomyData", snapshot.model_dump_json(), ex=get_config()["macroeconomy"]["cache_ttl"])
        return snapshot

    mdata = await asyncio.to_thread(_fetch_macroeconomy_data)
    await _cache_and_persist_macroeconomy_data(mdata)
    return mdata
