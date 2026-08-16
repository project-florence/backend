"""Optional FRED-based macroeconomy data, with lazy initialization.

Design spec 8.4: FRED is initialized only when ``FRED_API_KEY`` is present —
the import-time ``ValueError`` is gone, so the application boots without the
key. Read order: Redis cache -> 7-day DB snapshot -> lazy FRED fetch -> ``None``
(API answers "no data" — never a 500). ``_fetch_lock`` single-flights the
fetch; ``_fred_lock`` guards the lazy-init probe.
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial

from dotenv import load_dotenv
from fredapi import Fred
from pydantic import BaseModel

from src.core.config import get_config
from src.core.database import db
from src.core.redis import r

logger = logging.getLogger(__name__)


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

# Lazy FRED init (design spec 8.4): module import never touches the API key.
_fred: Fred | None = None
_fred_lock = asyncio.Lock()


def _get_fred() -> Fred | None:
    """Initialize FRED lazily — only when a key is configured.

    Idempotent first-call-wins assignment (GIL-protected); ``_fred_lock``
    serializes the probe from concurrent coroutines.
    """
    global _fred
    if _fred is not None:
        return _fred
    key = os.getenv("FRED_API_KEY")
    if not key:
        return None
    _fred = Fred(api_key=key)
    return _fred


def _get_latest_fred_val(fred_client: Fred, series_id: str, observation_start: str) -> float:
    # observation_start: serinin tamamini indirmek yerine sadece son 3 yili
    # isteriz; 14 seri indirmesi saniyelere iner (gunluk seri icin yeterli).
    series = fred_client.get_series(series_id, observation_start=observation_start).dropna()
    return float(series.iloc[-1])


def _fetch_macroeconomy_data() -> MacroeconomyData | None:
    """FRED-backed fetch. Returns None when FRED is unavailable (lazy init)."""
    fred_client = _get_fred()
    if fred_client is None:
        return None
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
    observation_start = (datetime.now(timezone.utc) - timedelta(days=3 * 365)).strftime("%Y-%m-%d")
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = dict(zip(
            series_ids,
            executor.map(
                partial(_get_latest_fred_val, fred_client, observation_start=observation_start),
                series_ids.values(),
            ),
        ))
    return MacroeconomyData(**values)


async def _load_recent_database_snapshot() -> MacroeconomyData | None:
    # Redis down iken DB snapshot'i guvenilir donsun: makro veri gunluk seri,
    # 7 gunluk tolerans yeterli (cache_ttl = 1 gun olabilir).
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
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


# Single-flight: Redis bos ve DB snapshot yoksa FRED'e ayni anda yalnizca
# tek fetch gitsin; eszamanli istekler kilidin ardinda bekler.
_fetch_lock = asyncio.Lock()


async def get_macroeconomy_data() -> MacroeconomyData | None:
    """Cache -> 7-day DB snapshot -> lazy FRED -> None ("veri yok", never 500)."""
    mdata = await r.get("MacroeconomyData")
    if mdata:
        try:
            return MacroeconomyData.model_validate_json(mdata)
        except Exception:
            pass

    snapshot = await _load_recent_database_snapshot()
    if snapshot:
        await r.set("MacroeconomyData", snapshot.model_dump_json(), ex=get_config()["macroeconomy"]["cache_ttl"])
        return snapshot

    async with _fred_lock:
        fred_available = _get_fred() is not None
    if not fred_available:
        return None

    async with _fetch_lock:
        # Kilit ardinda bekleyen istekler icin double-check: onceden fetch
        # tamamlanmissa tekrar FRED'e gitme.
        mdata = await r.get("MacroeconomyData")
        if mdata:
            try:
                return MacroeconomyData.model_validate_json(mdata)
            except Exception:
                pass

        snapshot = await _load_recent_database_snapshot()
        if snapshot:
            await r.set("MacroeconomyData", snapshot.model_dump_json(), ex=get_config()["macroeconomy"]["cache_ttl"])
            return snapshot

        try:
            mdata = await asyncio.to_thread(_fetch_macroeconomy_data)
        except Exception as exc:
            # FRED gecici hata: tek istekte 500 degil — bir sonraki tura birak.
            logger.warning("FRED macroeconomy fetch failed: %s", exc)
            return None
        if mdata is None:
            return None
        try:
            await _cache_and_persist_macroeconomy_data(mdata)
        except Exception as exc:
            # Cache/snapshot hala gecerli — persist hatasi istegi oldurmesin.
            logger.warning("macroeconomy persist failed: %s", exc)
        return mdata