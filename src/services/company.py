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


def _fmt_num(val) -> str:
    if val is None:
        return "-"
    if isinstance(val, (int, float)):
        if abs(val) >= 1_000_000_000_000:
            return f"{val / 1_000_000_000_000:.2f}T"
        if abs(val) >= 1_000_000_000:
            return f"{val / 1_000_000_000:.2f}B"
        if abs(val) >= 1_000_000:
            return f"{val / 1_000_000:.2f}M"
        if abs(val) >= 1_000:
            return f"{val:,.2f}"
        return f"{val:.2f}"
    return str(val)


def _fmt_pct(val) -> str:
    if val is None:
        return "-"
    return f"{val * 100:.2f}%" if isinstance(val, float) and abs(val) < 1 else f"{val:.2f}%"


def _fmt_price(val, currency="TRY") -> str:
    if val is None:
        return "-"
    return f"{_fmt_num(val)} {currency}"


def company_info_to_md(profile: dict) -> str:
    if not profile:
        return "No data available."

    symbol = profile.get("symbol", "")
    name = profile.get("name", "")
    sector = profile.get("sector", "-")
    industry = profile.get("industry", "-")
    exchange = profile.get("exchange", "-")
    currency = profile.get("currency", "TRY")

    m = profile.get("market", {})
    t = profile.get("trading", {})
    v = profile.get("valuation", {})
    f = profile.get("financials", {})
    b = profile.get("balanceSheet", {})
    recs = profile.get("recommendations", [])

    lines = [f"# {name} ({symbol})", f"**Sector:** {sector} | **Industry:** {industry}", f"**Exchange:** {exchange} | **Currency:** {currency}", ""]

    lines.append("## Market Data")
    lines.append(f"- **Current Price:** {_fmt_price(m.get('currentPrice'), currency)}")
    lines.append(f"- **Market Cap:** {_fmt_num(m.get('marketCap'))} {currency}")
    lines.append(f"- **Day Range:** {_fmt_price(m.get('dayLow'))} - {_fmt_price(m.get('dayHigh'))}")
    lines.append(f"- **52 Week Range:** {_fmt_price(m.get('fiftyTwoWeekLow'))} - {_fmt_price(m.get('fiftyTwoWeekHigh'))}")
    lines.append(f"- **Volume:** {_fmt_num(m.get('regularMarketVolume'))}")
    lines.append("")

    lines.append("## Trading Metrics")
    lines.append(f"- **Beta:** {t.get('beta', '-')}")
    lines.append(f"- **Shares Outstanding:** {_fmt_num(t.get('sharesOutstanding'))}")
    lines.append(f"- **Float Shares:** {_fmt_num(t.get('floatShares'))}")
    lines.append(f"- **Avg Volume:** {_fmt_num(t.get('averageVolume'))}")
    lines.append(f"- **50 Day Average:** {_fmt_price(t.get('fiftyDayAverage'))}")
    lines.append(f"- **200 Day Average:** {_fmt_price(t.get('twoHundredDayAverage'))}")
    lines.append(f"- **Insider Ownership:** {_fmt_pct(t.get('heldPercentInsiders'))}")
    lines.append(f"- **Institutional Ownership:** {_fmt_pct(t.get('heldPercentInstitutions'))}")
    lines.append("")

    lines.append("## Valuation")
    lines.append(f"- **Trailing P/E:** {v.get('trailingPE', '-')}")
    lines.append(f"- **Forward P/E:** {v.get('forwardPE', '-')}")
    lines.append(f"- **Price/Book:** {_fmt_num(v.get('priceToBook'))}")
    lines.append(f"- **Price/Sales (TTM):** {_fmt_num(v.get('priceToSalesTrailing12Months'))}")
    lines.append(f"- **Enterprise Value:** {_fmt_num(v.get('enterpriseValue'))} {currency}")
    lines.append(f"- **EV/EBITDA:** {_fmt_num(v.get('enterpriseToEbitda'))}")
    lines.append(f"- **EV/Revenue:** {_fmt_num(v.get('enterpriseToRevenue'))}")
    lines.append(f"- **Book Value:** {_fmt_price(v.get('bookValue'))}")
    lines.append(f"- **Trailing EPS:** {_fmt_price(v.get('trailingEps'), '')}")
    lines.append(f"- **Dividend Yield:** {_fmt_pct(v.get('dividendYield'))}")
    lines.append(f"- **Payout Ratio:** {_fmt_pct(v.get('payoutRatio'))}")
    target = v.get("targetMeanPrice")
    if target is not None:
        low = v.get("targetLowPrice", "-")
        high = v.get("targetHighPrice", "-")
        lines.append(f"- **Target Price:** {_fmt_price(target)} (Range: {_fmt_price(low)} - {_fmt_price(high)})")
    lines.append(f"- **Analyst Opinions:** {v.get('numberOfAnalystOpinions', '-')}")
    lines.append("")

    lines.append("## Financials")
    lines.append(f"- **Total Revenue:** {_fmt_num(f.get('totalRevenue'))} {currency}")
    lines.append(f"- **Revenue Growth:** {_fmt_pct(f.get('revenueGrowth'))}")
    lines.append(f"- **Gross Margin:** {_fmt_pct(f.get('grossMargins'))}")
    lines.append(f"- **EBITDA:** {_fmt_num(f.get('ebitda'))} {currency}")
    lines.append(f"- **Net Income:** {_fmt_num(f.get('netIncomeToCommon'))} {currency}")
    lines.append(f"- **Profit Margin:** {_fmt_pct(f.get('profitMargins'))}")
    lines.append(f"- **Operating Margin:** {_fmt_pct(f.get('operatingMargins'))}")
    lines.append(f"- **Free Cash Flow:** {_fmt_num(f.get('freeCashflow'))} {currency}")
    lines.append(f"- **Earnings Growth:** {_fmt_pct(f.get('earningsGrowth'))}")
    lines.append(f"- **ROE:** {_fmt_pct(f.get('returnOnEquity'))}")
    lines.append(f"- **ROA:** {_fmt_pct(f.get('returnOnAssets'))}")
    lines.append("")

    lines.append("## Balance Sheet")
    lines.append(f"- **Total Cash:** {_fmt_num(b.get('totalCash'))} {currency}")
    lines.append(f"- **Total Debt:** {_fmt_num(b.get('totalDebt'))} {currency}")
    lines.append(f"- **Debt/Equity:** {_fmt_pct(b.get('debtToEquity'))}")
    lines.append(f"- **Current Ratio:** {_fmt_num(b.get('currentRatio'))}")
    lines.append(f"- **Quick Ratio:** {_fmt_num(b.get('quickRatio'))}")
    lines.append("")

    if recs:
        lines.append("## Analyst Recommendations")
        header = "| Period | Strong Buy | Buy | Hold | Sell | Strong Sell |"
        sep = "|---|---|---|---|---|---|"
        rows = [f"| {r.get('period', '')} | {r.get('strongBuy', 0)} | {r.get('buy', 0)} | {r.get('hold', 0)} | {r.get('sell', 0)} | {r.get('strongSell', 0)} |" for r in recs]
        lines.append(header)
        lines.append(sep)
        lines.extend(rows)
        lines.append("")

    return "\n".join(lines)


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
        "trading": {
            "beta": raw_info.get("beta"),
            "sharesOutstanding": raw_info.get("sharesOutstanding"),
            "floatShares": raw_info.get("floatShares"),
            "averageVolume": raw_info.get("averageVolume"),
            "averageVolume10days": raw_info.get("averageVolume10days"),
            "fiftyDayAverage": raw_info.get("fiftyDayAverage"),
            "twoHundredDayAverage": raw_info.get("twoHundredDayAverage"),
            "shortRatio": raw_info.get("shortRatio"),
            "heldPercentInsiders": raw_info.get("heldPercentInsiders"),
            "heldPercentInstitutions": raw_info.get("heldPercentInstitutions"),
        },
        "valuation": {
            "trailingPE": raw_info.get("trailingPE"),
            "forwardPE": raw_info.get("forwardPE"),
            "pegRatio": raw_info.get("pegRatio") or raw_info.get("trailingPegRatio"),
            "priceToBook": raw_info.get("priceToBook"),
            "priceToSalesTrailing12Months": raw_info.get("priceToSalesTrailing12Months"),
            "enterpriseValue": raw_info.get("enterpriseValue"),
            "enterpriseToEbitda": raw_info.get("enterpriseToEbitda"),
            "enterpriseToRevenue": raw_info.get("enterpriseToRevenue"),
            "bookValue": raw_info.get("bookValue"),
            "trailingEps": raw_info.get("trailingEps"),
            "forwardEps": raw_info.get("forwardEps"),
            "dividendYield": raw_info.get("dividendYield"),
            "payoutRatio": raw_info.get("payoutRatio"),
            "targetMeanPrice": raw_info.get("targetMeanPrice"),
            "targetHighPrice": raw_info.get("targetHighPrice"),
            "targetLowPrice": raw_info.get("targetLowPrice"),
            "recommendationKey": raw_info.get("recommendationKey"),
            "numberOfAnalystOpinions": raw_info.get("numberOfAnalystOpinions"),
        },
        "financials": {
            "totalRevenue": raw_info.get("totalRevenue"),
            "revenuePerShare": raw_info.get("revenuePerShare"),
            "revenueGrowth": raw_info.get("revenueGrowth"),
            "grossProfits": raw_info.get("grossProfits"),
            "grossMargins": raw_info.get("grossMargins"),
            "ebitda": raw_info.get("ebitda"),
            "ebitdaMargins": raw_info.get("ebitdaMargins"),
            "netIncomeToCommon": raw_info.get("netIncomeToCommon"),
            "profitMargins": raw_info.get("profitMargins"),
            "operatingMargins": raw_info.get("operatingMargins"),
            "operatingCashflow": raw_info.get("operatingCashflow"),
            "freeCashflow": raw_info.get("freeCashflow"),
            "earningsGrowth": raw_info.get("earningsGrowth"),
            "earningsQuarterlyGrowth": raw_info.get("earningsQuarterlyGrowth"),
            "returnOnEquity": raw_info.get("returnOnEquity"),
            "returnOnAssets": raw_info.get("returnOnAssets"),
        },
        "balanceSheet": {
            "totalCash": raw_info.get("totalCash"),
            "totalCashPerShare": raw_info.get("totalCashPerShare"),
            "totalDebt": raw_info.get("totalDebt"),
            "debtToEquity": raw_info.get("debtToEquity"),
            "currentRatio": raw_info.get("currentRatio"),
            "quickRatio": raw_info.get("quickRatio"),
        },
        "recommendations": raw_info.get("recommendations", []),
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


def get_companies_summary(limit: int = 50, offset: int = 0, sort: str = "popular", tickers_filter: list[str] | None = None) -> dict:
    bist_companies = get_bist_companies_as_dict_from_redis()
    company_map = {c["ticker"]: c.get("name", "") for c in bist_companies}

    if tickers_filter:
        ticker_list = [t.upper() for t in tickers_filter if t.upper() in company_map]
        total = len(ticker_list)
    elif sort == "popular":
        stats = get_all_stats()
        total = len(stats)
        ticker_list = [s["ticker"] for s in stats[offset:offset + limit]]
    else:
        total = len(company_map)
        ticker_list = sorted(company_map.keys())[offset:offset + limit]

    if not ticker_list:
        return {"data": [], "total": total}

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

    return {"data": results, "total": total}


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
