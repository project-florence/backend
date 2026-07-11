from fastapi import APIRouter, Query, HTTPException
import json

from src.services.bist import (
    get_bist_companies_as_dict_from_redis,
    get_bist_tickers_as_dict_from_redis,
    search_companies_by_text,
    is_valid_bist_ticker,
)
from src.services.company import get_company_info
from src.services.news import get_latest_news
from src.services.report import generate_quick_report, generate_deep_report
from src.services.economy import (
    get_gold_prices,
    get_silver_prices,
    get_currency_symbols,
    get_currency,
)
from src.services.ipo import get_upcoming_ipos
from src.services.scout import scout_best_tickers
from src.services.price import get_price_history

router = APIRouter(prefix="/api/v1")


def _validate_ticker(ticker: str):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")


# ---------------------------------------------------------------------------
# BIST / Companies
# ---------------------------------------------------------------------------

@router.get("/bist/companies")
def bist_companies():
    return get_bist_companies_as_dict_from_redis()

@router.get("/bist/tickers")
def bist_tickers():
    return get_bist_tickers_as_dict_from_redis()

@router.get("/companies/search/{ticker}")
def search_companies(ticker: str):
    _validate_ticker(ticker)
    return search_companies_by_text(ticker)

@router.get("/companies/info/{ticker}")
def company_info(ticker: str):
    _validate_ticker(ticker)
    return get_company_info(ticker)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@router.get("/generate/report/quick/{ticker}")
def generate_quick_report_mock(ticker: str):
    _validate_ticker(ticker)
    return "Hizli Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit."

@router.get("/generate/report/deep/{ticker}")
def generate_deep_report_mock(ticker: str):
    _validate_ticker(ticker)
    return "Derin Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit."


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

@router.get("/news/{ticker}")
def news(ticker: str, amount: int = Query(default=10, description="Number of news items")):
    _validate_ticker(ticker)
    return get_latest_news(ticker, amount)


# ---------------------------------------------------------------------------
# Simulations
# ---------------------------------------------------------------------------

@router.get("/simulations/probability/{ticker}")
def probability(ticker: str, time: str = Query(...), target: str = Query(...)):
    _validate_ticker(ticker)
    return {"percent": 0.23, "ticker": ticker, "time": time, "target": target}

@router.get("/simulations/confidence-interval/{ticker}")
def confidence_interval(ticker: str, time: str = Query(...), bound: int = Query(...)):
    _validate_ticker(ticker)
    return {"min": 85, "max": 140, "currency": "TRY", "ticker": ticker, "time": time, "bound": bound}


# ---------------------------------------------------------------------------
# Economy
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Macro Economy
# ---------------------------------------------------------------------------

@router.get("/macroeconomy/all")
def macroeconomy_all():
    return {"henuz implement edilmedi. key value seklinde faiz, gdp, issizlik, cari acik, enflasyon vb. gibi verileri dondurur."}


# ---------------------------------------------------------------------------
# IPO
# ---------------------------------------------------------------------------

@router.get("/ipos/upcoming")
def ipos_upcoming():
    return json.loads(get_upcoming_ipos())


# ---------------------------------------------------------------------------
# Scout / Stock Picker
# ---------------------------------------------------------------------------

@router.get("/scout/best-tickers")
def best_tickers(
    investment_budget: int = Query(...),
    investment_horizon: str = Query(...),
    risk_tolerance: str = Query(...),
):
    return json.loads(scout_best_tickers())


# ---------------------------------------------------------------------------
# Price History
# ---------------------------------------------------------------------------

@router.get("/price/history/{ticker}")
def price_history(ticker: str, period: str = Query("1mo"), interval: str = Query("1d")):
    _validate_ticker(ticker)
    return get_price_history(ticker, period, interval)


# ---------------------------------------------------------------------------
# Auth (Mock)
# ---------------------------------------------------------------------------

@router.post("/auth/register")
def auth_register():
    return {"message": "Kayit olundu! Burada daha sonradan JWT alisverisi olacak."}

@router.post("/auth/login")
def auth_login():
    return {"message": "Giris yapildi! Burada daha sonradan JWT alisverisi olacak."}


# ---------------------------------------------------------------------------
# Favorites (Mock)
# ---------------------------------------------------------------------------

@router.post("/favorites/{ticker}")
def add_favorite(ticker: str):
    _validate_ticker(ticker)
    return {"message": f"{ticker} favorilere eklendi!"}

@router.delete("/favorites/{ticker}")
def remove_favorite(ticker: str):
    _validate_ticker(ticker)
    return {"message": f"{ticker} favorilerden cikarildi!"}

@router.get("/favorites")
def get_favorites():
    return {"favorites": ["ASELS", "THYAO", "GARAN", "KCHOL", "SISE"]}
