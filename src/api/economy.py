from typing import Optional

from fastapi import APIRouter, HTTPException
from src.services.economy import (
    get_gold_prices,
    get_silver_price,
    get_gram_platinum_price,
    get_gram_palladium_price,
    get_currency,
)
from src.clients.macroeconomy import get_macroeconomy_data

router = APIRouter()


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
    if mdata:
        return mdata
    raise HTTPException(status_code=500, detail="Internal server error")
