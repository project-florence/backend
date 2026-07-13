from typing import Optional

from fastapi import APIRouter
from src.services.economy import (
    get_gold_prices,
    get_silver_price,
    get_gram_platinum_price,
    get_gram_palladium_price,
    get_currency,
)

router = APIRouter()


@router.get("/economy/gold-prices")
def gold_prices():
    return get_gold_prices()


@router.get("/economy/silver-price")
def silver_prices():
    return get_silver_price()


@router.get("/economy/gram-platinum-price")
def gram_platinum_price():
    return get_gram_platinum_price()


@router.get("/economy/gram-palladium-price")
def gram_palladium_price():
    return get_gram_palladium_price()


@router.get("/economy/currency")
def currency(symbols: Optional[str] = None):
    data = get_currency()
    if symbols:
        keys = [s.strip().upper() for s in symbols.split(",")]
        return {k: data[k] for k in keys if k in data}
    return data


@router.get("/macroeconomy/all")
def macroeconomy_all():
    return {"henuz implement edilmedi. key value seklinde faiz, gdp, issizlik, cari acik, enflasyon vb. gibi verileri dondurur."}
