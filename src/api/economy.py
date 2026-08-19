"""Economy API — legacy bridge endpoints + canonical finance endpoints.

Legacy endpoints (gold-prices, silver-price, gram-platinum-price,
gram-palladium-price, currency) keep their paths and response keys
(``{Buying, Selling, Change, Type}``) but now serve numeric payloads through
the ``FinanceService`` bridge (design spec 8.1 / Faz 4). The canonical
endpoints (quotes, history, analysis, records, providers) expose the new
numeric contract (design spec 8.2). All routes stay behind the regular auth
middleware — none of them is added to PUBLIC_PATHS.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.clients.macroeconomy import get_macroeconomy_data
from src.finance import finance_service
from src.finance.models import AssetClass
from src.finance.symbols import SYMBOL_REGISTRY
from src.services.economy import (
    get_gold_prices,
    get_silver_price,
    get_gram_platinum_price,
    get_gram_palladium_price,
    get_currency,
)

router = APIRouter()

# period -> lookback window (design spec 8.2; "max" = full series).
_PERIOD_DAYS: dict[str, int] = {
    "1mo": 30,
    "3mo": 90,
    "6mo": 180,
    "1y": 365,
    "2y": 730,
    "5y": 1825,
}


def _require_known_symbol(symbol: str) -> str:
    """Normalize and validate a canonical symbol (404 on unknown)."""
    canonical = symbol.strip().upper()
    if canonical not in SYMBOL_REGISTRY:
        raise HTTPException(status_code=404, detail="Bilinmeyen sembol")
    return canonical


# ---------------------------------------------------------------------------
# Legacy endpoints (bridge — numeric payloads)
# ---------------------------------------------------------------------------


@router.get("/economy/gold-prices")
async def gold_prices():
    return await get_gold_prices()


@router.get("/economy/silver-price")
async def silver_prices():
    return await get_silver_price()


@router.get("/economy/gram-platinum-price")
async def gram_platinum_price():
    return await get_gram_platinum_price()


@router.get("/economy/gram-palladium-price")
async def gram_palladium_price():
    return await get_gram_palladium_price()


@router.get("/economy/currency")
async def currency(symbols: Optional[str] = None):
    data = await get_currency()
    if symbols:
        keys = [s.strip().upper() for s in symbols.split(",")]
        return {k: data[k] for k in keys if k in data}
    return data


@router.get("/macroeconomy")
async def macroeconomy_all():
    mdata = await get_macroeconomy_data()
    if mdata is None:
        # No FRED key and no recent DB snapshot: "no data", never a 500.
        raise HTTPException(status_code=404, detail="Veri yok")
    return mdata


# ---------------------------------------------------------------------------
# Canonical endpoints (numeric contract — design spec 8.2)
# ---------------------------------------------------------------------------


@router.get("/economy/quotes")
async def quotes(
    symbols: Optional[str] = None,
    group: Optional[str] = Query(None, description="fx | metal"),
):
    wanted = None
    if symbols:
        wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    bundle = await finance_service.get_quotes(wanted)
    if group:
        group_lower = group.lower()
        if group_lower not in ("fx", "metal"):
            raise HTTPException(status_code=400, detail="group 'fx' veya 'metal' olmali")
        wanted_class = AssetClass.FX if group_lower == "fx" else AssetClass.METAL
        bundle.quotes = {
            sym: q
            for sym, q in bundle.quotes.items()
            if SYMBOL_REGISTRY.get(sym) is not None
            and SYMBOL_REGISTRY[sym].asset_class is wanted_class
        }
    return bundle


@router.get("/economy/history/{symbol}")
async def history(
    symbol: str,
    period: str = Query("1y", description="1mo|3mo|6mo|1y|2y|5y|max"),
    interval: str = Query("1d"),
):
    canonical = _require_known_symbol(symbol)
    end = datetime.now(timezone.utc)
    start: datetime | None = None
    if period != "max":
        if period not in _PERIOD_DAYS:
            raise HTTPException(
                status_code=400,
                detail="period 1mo|3mo|6mo|1y|2y|5y|max olmali",
            )
        start = end - timedelta(days=_PERIOD_DAYS[period])
    candles = await finance_service.get_candles(canonical, interval, start, end)
    return candles


@router.get("/economy/analysis/{symbol}")
async def analysis(symbol: str):
    canonical = _require_known_symbol(symbol)
    result = await finance_service.get_analysis(canonical)
    if result is None:
        raise HTTPException(status_code=404, detail="Veri yok")
    return result


@router.get("/economy/records")
async def records():
    """Record/extreme summary for every FX and metal symbol (batched, no N+1)."""
    wanted = [
        symbol
        for symbol in sorted(SYMBOL_REGISTRY)
        if SYMBOL_REGISTRY[symbol].asset_class in (AssetClass.FX, AssetClass.METAL)
    ]
    out = await finance_service.get_records_many(wanted)
    # Drop symbols with no data at all (all fields null) to match the old shape.
    return {sym: rec for sym, rec in out.items() if any(v is not None for v in rec.values())}


@router.get("/economy/providers")
async def providers():
    """Source health panel (circuit state, last errors, quota)."""
    return await finance_service.get_provider_status()