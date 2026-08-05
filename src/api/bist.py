from fastapi import APIRouter, Query, Response, Depends, HTTPException
from src.services.bist import (
    get_bist_companies_as_dict_from_redis,
    get_bist_tickers_as_dict_from_redis,
    search_companies_by_text,
)
from src.services.company import get_company_info, get_companies_summary, company_info_to_md
from src.services.news import get_latest_news
from src.services.price import get_price_history
from src.services.quote import get_quote
from src.services.stats import increment_stat, get_popular_companies, get_popular_tickers
from src.api.deps import validate_ticker, get_current_user
from src.core.ratelimit import rate_limiter
from src.services.maintenance import require_feature

router = APIRouter()


@router.get("/bist/companies")
def bist_companies(
    sort: str = Query(default="alphabetical", description="Sort order: alphabetical or popular"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
    limit: int = Query(default=50, ge=1, le=500, description="Max items to return"),
):
    if sort == "popular":
        return get_popular_companies(n=offset + limit)[offset:]

    companies = get_bist_companies_as_dict_from_redis()
    return companies[offset: offset + limit]


@router.get("/bist/tickers")
def bist_tickers(
    sort: str = Query(default="alphabetical", description="Sort order: alphabetical or popular"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
    limit: int = Query(default=50, ge=1, le=500, description="Max items to return"),
):
    if sort == "popular":
        return get_popular_tickers(n=offset + limit)[offset:]

    tickers = get_bist_tickers_as_dict_from_redis()
    return tickers[offset: offset + limit]


@router.get("/companies/search")
def search_companies(query: str = Query(...)):
    return search_companies_by_text(query)


@router.get("/companies/info/{ticker}")
def company_info(ticker: str):
    validate_ticker(ticker)
    result = get_company_info(ticker)
    increment_stat(ticker, "info_count")
    return result


@router.get("/companies/info/{ticker}/md")
def company_info_md(ticker: str):
    validate_ticker(ticker)
    profile = get_company_info(ticker)
    return Response(content=company_info_to_md(profile), media_type="text/markdown; charset=utf-8")


@router.get("/companies/summary")
def companies_summary(
    limit: int = Query(default=50, ge=1, le=500, description="Number of companies"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
    sort: str = Query(default="popular", description="Sort order: popular, alphabetical, gainers, losers, price_high, price_low, volume, market_cap"),
    tickers: str | None = Query(default=None, description="Comma-separated ticker filter"),
):
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    return get_companies_summary(limit=limit, offset=offset, sort=sort, tickers_filter=ticker_list)


@router.get("/news/{ticker}")
def news(ticker: str, amount: int = Query(default=10, ge=1, le=50, description="Number of news items"), current_user_id: int = Depends(get_current_user), _: bool = Depends(require_feature("news"))):
    rate_limiter.check(f"news:{current_user_id}:{ticker.upper()}", max_requests=10, window_seconds=60)
    validate_ticker(ticker)
    result = get_latest_news(ticker, amount)
    increment_stat(ticker, "news_count")
    return result


_VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
_VALID_INTERVALS = {"5m", "30m", "1h", "1d", "5d", "1wk", "1mo", "3mo"}


@router.get("/price/history/{ticker}")
def price_history(ticker: str, period: str = Query("1mo"), interval: str = Query("1d")):
    validate_ticker(ticker)
    if period not in _VALID_PERIODS:
        raise HTTPException(status_code=400, detail=f"Invalid period. Must be one of: {', '.join(sorted(_VALID_PERIODS))}")
    if interval not in _VALID_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval. Must be one of: {', '.join(sorted(_VALID_INTERVALS))}")
    try:
        result = get_price_history(ticker, period, interval)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    increment_stat(ticker, "history_count")
    return result


@router.get("/price/current")
def current_price(
    ticker: str = Query(...),
    interval: str = Query(default="5m", description="Candle interval: 5m, 30m, 1h, 1d"),
):
    validate_ticker(ticker)
    if interval not in _VALID_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval. Must be one of: {', '.join(sorted(_VALID_INTERVALS))}")
    quote = get_quote(ticker.upper())
    if quote["price"] is None:
        raise HTTPException(status_code=404, detail="Price not found")
    return {**quote, "interval": interval, "price": quote["price"]}
