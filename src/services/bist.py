import json
import psycopg2.extras
import pykap
from src.core.redis import r
from src.core.config import get_config
from src.core.database import db


def _cache_interval():
    return get_config()["get_bist_companies"]["cache_interval"]


def _fetch_and_persist_all():
    tickers = pykap.bist_company_list()
    companies = pykap.get_bist_companies(output_format="dict")

    company_map = {c["ticker"]: c["name"] for c in companies}
    with db.cursor() as cur:
        for code in tickers:
            name = company_map.get(code)
            cur.execute(
                "INSERT INTO tickers (code, name, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, updated_at = NOW()",
                (code, name),
            )
        for c in companies:
            cur.execute(
                "INSERT INTO companies (ticker, name, summary_page, city, auditor, company_id, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, NOW()) "
                "ON CONFLICT (ticker) DO UPDATE SET "
                "name = EXCLUDED.name, summary_page = EXCLUDED.summary_page, "
                "city = EXCLUDED.city, auditor = EXCLUDED.auditor, "
                "company_id = EXCLUDED.company_id, updated_at = NOW()",
                (c["ticker"], c["name"], c["summary_page"], c["city"], c["auditor"], c["company_id"]),
            )
    db.commit()

    tickers_json = json.dumps(tickers)
    companies_json = json.dumps(companies)
    r.set("tickers", tickers_json, ex=_cache_interval())
    r.set("companies", companies_json, ex=_cache_interval())
    return tickers_json, companies_json


def _db_get_tickers():
    with db.cursor() as cur:
        cur.execute("SELECT code FROM tickers ORDER BY code")
        rows = cur.fetchall()
        if not rows:
            return None
        return json.dumps([row[0] for row in rows])


def _db_get_companies():
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM companies ORDER BY ticker")
        rows = cur.fetchall()
        if not rows:
            return None
        return json.dumps([dict(r) for r in rows])


def get_bist_tickers_as_list():
    return pykap.bist_company_list()


def get_bist_tickers_as_json():
    return json.dumps(pykap.bist_company_list())


def get_bist_tickers_as_json_from_redis():
    tickers = r.get("tickers")
    if tickers is not None:
        return tickers

    tickers = _db_get_tickers()
    if tickers is not None:
        r.set("tickers", tickers, ex=_cache_interval())
        return tickers

    tickers_json, _ = _fetch_and_persist_all()
    return tickers_json


def get_bist_tickers_as_dict_from_redis():
    return json.loads(get_bist_tickers_as_json_from_redis())


def get_bist_companies_as_dict():
    return pykap.get_bist_companies(output_format="dict")


def get_bist_companies_as_json():
    return pykap.get_bist_companies(output_format="json")


def get_bist_companies_as_json_from_redis():
    companies = r.get("companies")
    if companies is not None:
        return companies

    companies = _db_get_companies()
    if companies is not None:
        r.set("companies", companies, ex=_cache_interval())
        return companies

    _, companies_json = _fetch_and_persist_all()
    return companies_json


def get_bist_companies_as_dict_from_redis():
    return json.loads(get_bist_companies_as_json_from_redis())


def cache_tickers_and_companies():
    get_bist_companies_as_json_from_redis()
    get_bist_tickers_as_json_from_redis()


def sync_tickers_and_companies():
    _fetch_and_persist_all()


def search_companies_by_text(text, limit: int = 20):
    from src.services.search import search_companies as _search
    return _search(text, limit=limit)


def is_valid_bist_ticker(ticker: str) -> bool:
    ticker = ticker.upper()
    return ticker in get_bist_tickers_as_dict_from_redis()
