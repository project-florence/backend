from fastapi import FastAPI, APIRouter, Query, HTTPException

from src.get_bist_companies import get_bist_companies_as_dict_from_redis, get_bist_tickers_as_dict_from_redis, cache_tickers_and_companies
from src.article_collector import get_latest_news
from src.article_analyzer import generate_deep_report, generate_quick_report
from src.search_utils import search_companies_by_text
from src.config import init_config
from src.company_info import get_company_info
from src.validation_utils import is_valid_bist_ticker
import src.economy_utils as economy_utils
from src.ipo_utils import get_upcoming_ipos
from src.scout_utils import scout_best_tickers
from src.price_history import get_price_history
import json

app = FastAPI()
router = APIRouter(prefix="/api/v1")

# init server
init_config()
cache_tickers_and_companies()


# ---------------------------------------------------------------------------
# Root endpoints (prefix yok)
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {}

@app.get("/health")
def health():
    return {"status": "ok"}


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
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return search_companies_by_text(ticker)

@router.get("/companies/info/{ticker}")
def company_info(ticker: str):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return get_company_info(ticker)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@router.get("/generate/report/quick/{ticker}")
def generate_quick_report(ticker: str):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return """Hizli Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum."""

@router.get("/generate/report/deep/{ticker}")
def generate_deep_report(ticker: str):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return """Derin Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum."""


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

@router.get("/news/{ticker}")
def news(ticker: str, amount: int = Query(default=10, description="Number of news items")):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return get_latest_news(ticker, amount)


# ---------------------------------------------------------------------------
# Simulations
# ---------------------------------------------------------------------------

@router.get("/simulations/probability/{ticker}")
def probability(ticker: str, time: str = Query(...), target: str = Query(...)):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return {"percent": 0.23, "ticker": ticker, "time": time, "target": target}

@router.get("/simulations/confidence-interval/{ticker}")
def confidence_interval(ticker: str, time: str = Query(...), bound: int = Query(...)):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return {"min": 85, "max": 140, "currency": "TRY", "ticker": ticker, "time": time, "bound": bound}


# ---------------------------------------------------------------------------
# Economy
# ---------------------------------------------------------------------------

@router.get("/economy/gold-prices")
def gold_prices():
    return economy_utils.get_gold_prices()

@router.get("/economy/silver-prices")
def silver_prices():
    return economy_utils.get_silver_prices()

@router.get("/economy/symbols")
def symbols():
    return economy_utils.get_currency_symbols()

@router.get("/economy/currency")
def currency():
    return economy_utils.get_currency()


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
def best_tickers(investment_budget: int = Query(...), investment_horizon: str = Query(...), risk_tolerance: str = Query(...)):
    return scout_best_tickers()


# ---------------------------------------------------------------------------
# Price History
# ---------------------------------------------------------------------------

@router.get("/price/history/{ticker}")
def price_history(ticker: str, period: str = Query("1mo"), interval: str = Query("1d")):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
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
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return {"message": f"{ticker} favorilere eklendi!"}

@router.delete("/favorites/{ticker}")
def remove_favorite(ticker: str):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return {"message": f"{ticker} favorilerden cikarildi!"}

@router.get("/favorites")
def get_favorites():
    return {"favorites": ["ASELS", "THYAO", "GARAN", "KCHOL", "SISE"]}


# ---------------------------------------------------------------------------
# Router'i app'e bagla
# ---------------------------------------------------------------------------

app.include_router(router)
