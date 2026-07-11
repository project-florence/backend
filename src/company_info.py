# company_info.py
# written by bekir

import yfinance as yf
import json
import time
import random
from src.get_bist_companies import get_bist_tickers_as_dict_from_redis
from src.redis_connection import r
from src.config import get_config


# ---------------------------------------------------------------------------
# Yardimcilar
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Veri donusturme (pure) — yfinance'dan bagimsiz
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Veri cekme (sadece yfinance)
# ---------------------------------------------------------------------------

def fetch_from_yfinance(ticker_symbol: str, max_retries: int | None = None) -> dict:
    cfg = get_config()["company_info"]
    if max_retries is None:
        max_retries = cfg["max_retries"]

    for attempt in range(max_retries):
        try:
            info = yf.Ticker(ticker_symbol).info
            if info:
                return info
        except Exception as e:
            print(f"yfinance hatasi ({ticker_symbol}), deneme {attempt + 1}/{max_retries}: {e}")

        if attempt < max_retries - 1:
            time.sleep(random.uniform(cfg["retry_sleep_min"], cfg["retry_sleep_max"]))

    print(f"MAX RETRY asildi ({ticker_symbol})")
    return {}


# ---------------------------------------------------------------------------
# Ikisini birlestiren wrapper (geriye uyum)
# ---------------------------------------------------------------------------

def create_company_json(ticker_symbol: str, max_retries: int | None = None) -> dict:
    raw = fetch_from_yfinance(ticker_symbol, max_retries)
    return build_company_profile(raw)


# ---------------------------------------------------------------------------
# Tek hisse
# ---------------------------------------------------------------------------

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

    profile = create_company_json(ticker)
    if profile:
        _save_company_to_redis(ticker, profile)

    return profile


# ---------------------------------------------------------------------------
# Toplu islem
# ---------------------------------------------------------------------------

def cache_all_companies_to_redis(force_update: bool = False, background: bool = False) -> dict:
    if not background:
        print("Tum sirket verileri Redis'e cache'leniyor...")

    tickers = _get_bist_tickers()
    total = len(tickers)

    stats = {
        "success": True, "total": total,
        "successful": 0, "failed": 0, "cached": 0, "updated": 0,
    }
    start = time.time()

    cfg = get_config()["company_info"]

    for idx, ticker in enumerate(tickers, 1):
        was_cached = False

        if not force_update:
            try:
                if r.get(ticker):
                    stats["cached"] += 1
                    was_cached = True
                    continue
            except Exception:
                pass

        profile = create_company_json(ticker)

        if not profile:
            stats["failed"] += 1
            if not background:
                print(f"{ticker} icin veri alinamadi")
        elif _save_company_to_redis(ticker, profile):
            stats["successful"] += 1
            stats["updated"] += 1
            if not background:
                print(f"{ticker} cache'e eklendi/guncellendi")
        else:
            stats["failed"] += 1

        if not was_cached:
            delay = random.uniform(cfg["request_delay_min"], cfg["request_delay_max"])
            if idx % cfg["batch_size"] == 0 and idx < total:
                if not background:
                    print(f"Batch {idx // cfg['batch_size']} tamamlandi, {cfg['batch_delay']} sn bekleniyor...")
                time.sleep(cfg["batch_delay"])
            else:
                time.sleep(delay)

        if not background and idx % 50 == 0:
            print(f"--- {idx}/{total} tamamlandi. Basarili: {stats['successful']}, Basarisiz: {stats['failed']} ---")

    stats["elapsed_time"] = round(time.time() - start, 2)
    stats["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")

    if not background:
        print(f"Tamamlandi: {total} hisse, {stats['successful']} basarili, "
              f"{stats['failed']} basarisiz, {stats['cached']} cache ({stats['elapsed_time']} sn)")

    return stats
