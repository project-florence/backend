from fastapi import APIRouter
from src.services.economy import (
    get_gold_prices,
    get_silver_prices,
    get_currency_symbols,
    get_currency,
)

router = APIRouter()


@router.get("/economy/gold-prices")
def gold_prices():
    return get_gold_prices()


@router.get("/economy/silver-prices")
def silver_prices():
    return get_silver_prices()


@router.get("/economy/symbols")
def symbols():
    return get_currency_symbols()


@router.get("/economy/currency")
def currency():
    return get_currency()


@router.get("/macroeconomy/all")
def macroeconomy_all():
    return {"henuz implement edilmedi. key value seklinde faiz, gdp, issizlik, cari acik, enflasyon vb. gibi verileri dondurur."}
