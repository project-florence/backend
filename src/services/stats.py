from src.core.database import db
from src.services.bist import get_bist_tickers_as_dict_from_redis, get_bist_companies_as_dict_from_redis

STAT_FIELDS = [
    "info_count",
    "report_count",
    "news_count",
    "history_count",
    "simulation_count",
    "favorite_count",
]


def increment_stat(ticker: str, field: str) -> None:
    if field not in STAT_FIELDS:
        raise ValueError(f"Invalid stat field: {field}")

    ticker = ticker.upper()
    with db.cursor() as cur:
        cur.execute(
            f"""INSERT INTO ticker_stats (ticker, {field}, updated_at)
                VALUES (%s, 1, NOW())
                ON CONFLICT (ticker)
                DO UPDATE SET {field} = ticker_stats.{field} + 1, updated_at = NOW()""",
            (ticker,),
        )
        db.commit()


def get_ticker_stats(ticker: str) -> dict:
    ticker = ticker.upper()
    with db.cursor() as cur:
        cur.execute(
            """SELECT info_count, report_count, news_count, history_count,
                      simulation_count, favorite_count
               FROM ticker_stats WHERE ticker = %s""",
            (ticker,),
        )
        row = cur.fetchone()
        if not row:
            return {f: 0 for f in STAT_FIELDS}
        return dict(zip(STAT_FIELDS, row))


def get_top_tickers(limit: int = 50) -> list[dict]:
    ticker_names = _get_ticker_name_map()

    with db.cursor() as cur:
        cur.execute(
            f"""SELECT ticker,
                       info_count, report_count, news_count, history_count,
                       simulation_count, favorite_count,
                       info_count + report_count + news_count + history_count + simulation_count + favorite_count AS total
                FROM ticker_stats
                ORDER BY total DESC
                LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()

    results = []
    for row in rows:
        ticker = row[0]
        counts = {f: row[i + 1] for i, f in enumerate(STAT_FIELDS)}
        counts["total"] = row[-1]
        results.append({
            "ticker": ticker,
            "name": ticker_names.get(ticker, ""),
            **counts,
        })
    return results


def get_all_stats() -> list[dict]:
    ticker_names = _get_ticker_name_map()
    all_tickers = get_bist_tickers_as_dict_from_redis()

    with db.cursor() as cur:
        cur.execute("SELECT ticker, info_count, report_count, news_count, history_count, simulation_count, favorite_count FROM ticker_stats")
        rows = cur.fetchall()

    db_stats = {}
    for row in rows:
        ticker = row[0]
        db_stats[ticker] = {f: row[i + 1] for i, f in enumerate(STAT_FIELDS)}

    results = []
    for ticker in all_tickers:
        stats = db_stats.get(ticker, {f: 0 for f in STAT_FIELDS})
        total = sum(stats.values())
        results.append({
            "ticker": ticker,
            "name": ticker_names.get(ticker, ""),
            **stats,
            "total": total,
        })

    results.sort(key=lambda x: -x["total"])
    return results


def get_popular_tickers(n: int = 10) -> list[str]:
    stats = get_all_stats()
    return [s["ticker"] for s in stats[:n]]


def get_popular_companies(n: int = 10) -> list[dict]:
    companies = get_bist_companies_as_dict_from_redis()
    stats = get_all_stats()
    ticker_order = {s["ticker"]: i for i, s in enumerate(stats)}
    companies.sort(key=lambda c: (ticker_order.get(c["ticker"], 999), c["ticker"]))
    return companies[:n]


def get_popular_company_summaries(n: int = 10) -> list[dict]:
    from src.services.company import get_companies_summary
    tickers = get_popular_tickers(n)
    return get_companies_summary(limit=n, tickers_filter=tickers)


def _get_ticker_name_map() -> dict[str, str]:
    try:
        companies = get_bist_companies_as_dict_from_redis()
        return {c["ticker"]: c.get("name", "") for c in companies}
    except Exception:
        return {}
