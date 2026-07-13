from fastapi import APIRouter, Query
from src.services.bist import (
    get_bist_companies_as_dict_from_redis,
    get_bist_tickers_as_dict_from_redis,
    search_companies_by_text,
)
from src.services.company import get_company_info, get_companies_summary
from src.services.news import get_latest_news
from src.services.price import get_price_history
from src.services.stats import increment_stat, get_all_stats
from src.api.deps import validate_ticker

router = APIRouter()


@router.get("/bist/companies")
def bist_companies(sort: str = Query(default="alphabetical", description="Sort order: alphabetical or popular")):
    companies = get_bist_companies_as_dict_from_redis()
    if sort == "popular":
        stats = get_all_stats()
        ticker_order = {s["ticker"]: i for i, s in enumerate(stats)}
        companies.sort(key=lambda c: (ticker_order.get(c["ticker"], 999), c["ticker"]))
    return companies


@router.get("/bist/tickers")
def bist_tickers(sort: str = Query(default="alphabetical", description="Sort order: alphabetical or popular")):
    tickers = get_bist_tickers_as_dict_from_redis()
    if sort == "popular":
        stats = get_all_stats()
        ticker_order = {s["ticker"]: i for i, s in enumerate(stats)}
        tickers.sort(key=lambda t: (ticker_order.get(t, 999), t))
    return tickers


@router.get("/companies/search")
def search_companies(query: str = Query(...)):
    return search_companies_by_text(query)


@router.get("/companies/info/{ticker}")
def company_info(ticker: str):
    validate_ticker(ticker)
    result = get_company_info(ticker)
    increment_stat(ticker, "info_count")
    return result


@router.get("/companies/summary")
def companies_summary(
    limit: int = Query(default=50, description="Number of companies"),
    sort: str = Query(default="popular", description="Sort order: popular or alphabetical"),
    tickers: str | None = Query(default=None, description="Comma-separated ticker filter"),
):
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    return get_companies_summary(limit=limit, sort=sort, tickers_filter=ticker_list)


@router.get("/news/{ticker}")
def news(ticker: str, amount: int = Query(default=10, description="Number of news items")):
    validate_ticker(ticker)
    result = get_latest_news(ticker, amount)
    increment_stat(ticker, "news_count")
    return result


@router.get("/price/history/{ticker}")
def price_history(ticker: str, period: str = Query("1mo"), interval: str = Query("1d")):
    validate_ticker(ticker)
    result = get_price_history(ticker, period, interval)
    increment_stat(ticker, "history_count")
    return result
