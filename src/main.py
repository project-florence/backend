from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel
from typing import Optional
from src.get_bist_companies import get_bist_companies_as_dict_from_redis, get_bist_tickers_as_dict_from_redis, cache_tickers_and_companies
from src.article_collector import get_latest_news
from src.article_analyzer import generate_deep_report, generate_quick_report
from src.search_utils import search_companies_by_text
from src.config import init_config
from src.company_info import get_company_info
from src.validation_utils import is_valid_bist_ticker
import json

app = FastAPI()

# init server
init_config()
cache_tickers_and_companies()

@app.get("/")
def root():
    return {}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/bist/companies")
def bist_companies():
    return get_bist_companies_as_dict_from_redis()

@app.get("/bist/tickers")
def bist_tickers():
    return get_bist_tickers_as_dict_from_redis()

@app.get("/companies/search/{ticker}")
def search_companies(ticker: str):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return search_companies_by_text(ticker)

@app.get("/companies/info/{ticker}")
def company_info(ticker: str):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return get_company_info(ticker)
@app.get("/generate/report/quick/{ticker}")
def generate_quick_report(ticker: str):
    return """Hizli Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum."""

@app.get("/generate/report/deep/{ticker}")
def generate_deep_report(ticker: str):
    return """Derin Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum."""

# Probability Endpoint
@app.get("/simulations/probability/{ticker}")
def probability(ticker: str, time: str = Query(...), target: str = Query(...)):
    return {
        "percent": 0.23,
        "ticker": ticker,
        "time": time,
        "target": target
    }

# Confidence Interval Endpoint
@app.get("/simulations/confidence-interval/{ticker}")
def confidence_interval(ticker: str, time: str = Query(...), bound: int = Query(...)):
    return {
        "min": 85,
        "max": 140,
        "type": "tl",
        "ticker": ticker,
        "time": time,
        "bound": bound
    }

@app.get("/news/{ticker}")
def news(ticker: str, amount: int = Query(default=10, description="Number of news items")):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
    return get_latest_news(ticker, amount)