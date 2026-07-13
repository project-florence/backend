from fastapi import APIRouter, Query
from src.services.stats import get_top_tickers, get_ticker_stats
from src.api.deps import validate_ticker

router = APIRouter()


@router.get("/stats/top")
def stats_top(limit: int = Query(default=50, description="Number of top tickers")):
    return get_top_tickers(limit=limit)


@router.get("/stats/{ticker}")
def stats_ticker(ticker: str):
    validate_ticker(ticker)
    stats = get_ticker_stats(ticker)
    stats["ticker"] = ticker.upper()
    return stats
