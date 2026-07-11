import json
import pykap
from src.redis_connection import r
from src.config import get_config
import redis


def _cache_interval():
    return get_config()["get_bist_companies"]["cache_interval"]


def get_bist_tickers_as_list():
    return pykap.bist_company_list()


def get_bist_tickers_as_json():
    return json.dumps(pykap.bist_company_list())


def get_bist_tickers_as_json_from_redis():
    tickers = r.get("tickers")
    if tickers is None:
        print("coming from api")
        tickers = get_bist_tickers_as_json()
        r.set("tickers", tickers, ex=_cache_interval())
    else:
        print("coming from redis")
    return tickers


def get_bist_tickers_as_dict_from_redis():
    tickers = r.get("tickers")
    if tickers is None:
        print("coming from api")
        tickers = get_bist_tickers_as_json()
        r.set("tickers", tickers, ex=_cache_interval())
    else:
        print("coming from redis")
    return json.loads(tickers)


def get_bist_companies_as_dict():
    return pykap.get_bist_companies(output_format='dict')


def get_bist_companies_as_json():
    return pykap.get_bist_companies(output_format='json')


def get_bist_companies_as_json_from_redis():
    companies = r.get("companies")
    if companies is None:
        print("coming from api")
        companies = get_bist_companies_as_json()
        r.set("companies", companies, ex=_cache_interval())
    else:
        print("coming from redis")
    return companies


def get_bist_companies_as_dict_from_redis():
    companies = r.get("companies")
    if companies is None:
        print("coming from api")
        companies = get_bist_companies_as_json()
        r.set("companies", companies, ex=_cache_interval())
    else:
        print("coming from redis")
    return json.loads(companies)


def cache_tickers_and_companies():
    get_bist_companies_as_json_from_redis()
    get_bist_tickers_as_json_from_redis()
