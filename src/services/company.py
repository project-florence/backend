import json
import time
import random
from datetime import datetime, timezone
from src.core.redis import r
from src.core.config import get_config
from src.clients.yfinance import fetch_company_info
from src.services.bist import get_bist_tickers_as_dict_from_redis, get_bist_companies_as_dict_from_redis
from src.services.stats import get_all_stats
from src.core.database import db


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


def get_companies_summary(limit: int = 50, offset: int = 0, sort: str = "popular", tickers_filter: list[str] | None = None) -> list[dict]:
    bist_companies = get_bist_companies_as_dict_from_redis()
    company_map = {c["ticker"]: c.get("name", "") for c in bist_companies}

    if tickers_filter:
        ticker_list = [t.upper() for t in tickers_filter if t.upper() in company_map]
    elif sort == "popular":
        stats = get_all_stats()
        ticker_list = [s["ticker"] for s in stats[offset:offset + limit]]
    else:
        ticker_list = sorted(company_map.keys())[offset:offset + limit]

    if not ticker_list:
        return []

    latest_candles = _fetch_latest_candles(ticker_list)

    results = []
    for ticker in ticker_list:
        key = f"{ticker}.IS"
        cached = None
        try:
            cached_raw = r.get(key)
            if cached_raw:
                cached = json.loads(cached_raw)
        except Exception:
            pass

        candle = latest_candles.get(key)

        last_price = None
        change_pct = None
        day_high = None
        day_low = None
        volume = None
        market_cap = None
        sector = None
        price_updated_at = None

        if cached:
            mkt = cached.get("market", {}) or {}
            last_price = mkt.get("currentPrice")
            day_high = mkt.get("dayHigh")
            day_low = mkt.get("dayLow")
            volume = mkt.get("regularMarketVolume")
            market_cap = mkt.get("marketCap")
            sector = cached.get("sector")

        if candle:
            if last_price is None:
                last_price = candle["close"]
                day_high = candle["high"]
                day_low = candle["low"]
                volume = candle["volume"]
            if candle["open"] and candle["open"] != 0:
                change_pct = round((candle["close"] - candle["open"]) / candle["open"] * 100, 2)

        if cached:
            price_updated_at = datetime.now(timezone.utc).isoformat()
        elif candle:
            price_updated_at = candle["ts"].isoformat() if candle["ts"] else None

        results.append({
            "ticker": ticker,
            "name": company_map.get(ticker, ""),
            "sector": sector,
            "last_price": last_price,
            "change_pct": change_pct,
            "day_high": day_high,
            "day_low": day_low,
            "volume": volume,
            "market_cap": market_cap,
            "currency": "TRY",
            "price_updated_at": price_updated_at,
        })

    return results


def _fetch_latest_candles(ticker_list: list[str]) -> dict[str, dict]:
    if not ticker_list:
        return {}

    tickers_is = [t + ".IS" for t in ticker_list]
    placeholders = ",".join(["%s"] * len(tickers_is))

    with db.cursor() as cur:
        cur.execute(
            f"""SELECT DISTINCT ON (ticker) ticker, ts, open, high, low, close, volume
                FROM price_candles
                WHERE ticker IN ({placeholders}) AND interval = '1d'
                ORDER BY ticker, ts DESC""",
            tickers_is,
        )
        rows = cur.fetchall()

    return {
        r[0]: {"ts": r[1], "open": r[2], "high": r[3], "low": r[4], "close": r[5], "volume": r[6]}
        for r in rows
    }
