import json
import time
import random
from src.core.redis import r
from src.core.config import get_config
from src.clients.yfinance import fetch_company_info
from src.services.bist import get_bist_tickers_as_dict_from_redis


def _get_bist_tickers() -> list[str]:
    return [t + ".IS" for t in get_bist_tickers_as_dict_from_redis()]


def _save_company_to_redis(ticker: str, profile: dict) -> bool:
    try:
        cfg = get_config()["company_info"]
        r.set(ticker, json.dumps(profile), ex=cfg["cache_ttl"])
        return True
    except Exception as e:
        print(f"Redis kaydetme hatasi ({ticker}): {e}")
        return False


def build_company_profile(raw_info: dict) -> dict:
    if not raw_info:
        return {}

    return {
        "symbol": raw_info.get("symbol"),
        "name": raw_info.get("shortName") or raw_info.get("longName"),
        "sector": raw_info.get("sector"),
        "industry": raw_info.get("industry"),
        "currency": raw_info.get("currency"),
        "exchange": raw_info.get("exchange"),
        "market": {
            "currentPrice": raw_info.get("currentPrice"),
            "marketCap": raw_info.get("marketCap"),
            "dayHigh": raw_info.get("dayHigh"),
            "dayLow": raw_info.get("dayLow"),
            "regularMarketVolume": raw_info.get("regularMarketVolume"),
            "fiftyTwoWeekHigh": raw_info.get("fiftyTwoWeekHigh"),
            "fiftyTwoWeekLow": raw_info.get("fiftyTwoWeekLow"),
        },
        "valuation": {
            "trailingPE": raw_info.get("trailingPE"),
            "forwardPE": raw_info.get("forwardPE"),
            "priceToBook": raw_info.get("priceToBook"),
            "dividendYield": raw_info.get("dividendYield"),
            "payoutRatio": raw_info.get("payoutRatio"),
            "targetMeanPrice": raw_info.get("targetMeanPrice"),
            "targetHighPrice": raw_info.get("targetHighPrice"),
            "targetLowPrice": raw_info.get("targetLowPrice"),
        },
        "financials": {
            "totalRevenue": raw_info.get("totalRevenue"),
            "grossProfits": raw_info.get("grossProfits"),
            "netIncomeToCommon": raw_info.get("netIncomeToCommon"),
            "profitMargins": raw_info.get("profitMargins"),
            "operatingMargins": raw_info.get("operatingMargins"),
            "revenueGrowth": raw_info.get("revenueGrowth"),
            "earningsGrowth": raw_info.get("earningsGrowth"),
            "returnOnEquity": raw_info.get("returnOnEquity"),
            "ebitda": raw_info.get("ebitda"),
        },
        "balanceSheet": {
            "totalCash": raw_info.get("totalCash"),
            "totalDebt": raw_info.get("totalDebt"),
            "debtToEquity": raw_info.get("debtToEquity"),
            "currentRatio": raw_info.get("currentRatio"),
        },
    }


def get_company_info(ticker: str, use_cache: bool = True) -> dict:
    ticker = ticker.upper()
    if not ticker.endswith(".IS"):
        ticker = f"{ticker}.IS"

    if use_cache:
        try:
            cached = r.get(ticker)
            if cached:
                return json.loads(cached)
        except Exception as e:
            print(f"Redis okuma hatasi: {e}")

    raw = fetch_company_info(ticker)
    profile = build_company_profile(raw)
    if profile:
        _save_company_to_redis(ticker, profile)

    return profile
