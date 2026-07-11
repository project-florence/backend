from src.core.config import get_config
from src.core.redis import r
from dotenv import load_dotenv
import os
import json
import requests

load_dotenv()
api_key = os.getenv("COLLECT_API_KEY")


def _cache_key(endpoint: str) -> str:
    return f"economy:{endpoint.strip('/').replace('/', '_')}"


def _send_request(url: str):
    headers = {'content-type': "application/json", 'authorization': f"apikey {api_key}"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()["result"]
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}


def _fetch_or_cache(endpoint: str, url: str) -> dict | list:
    key = _cache_key(endpoint)
    cached = r.get(key)
    if cached:
        return json.loads(cached)

    result = _send_request(url)
    if result and "error" not in result:
        cfg = get_config()["economy"]
        r.set(key, json.dumps(result, ensure_ascii=False), ex=cfg["cache_ttl"])
    return result


def get_gold_prices():
    cfg = get_config()["economy"]
    url = f"https://{cfg['api_url']}{cfg['gold_endpoint']}"
    return _fetch_or_cache(cfg["gold_endpoint"], url)


def get_silver_prices():
    cfg = get_config()["economy"]
    url = f"https://{cfg['api_url']}{cfg['silver_endpoint']}"
    return _fetch_or_cache(cfg["silver_endpoint"], url)


def get_currency_symbols():
    cfg = get_config()["economy"]
    url = f"https://{cfg['api_url']}{cfg['currency_symbols_endpoint']}"
    return _fetch_or_cache(cfg["currency_symbols_endpoint"], url)


def get_currency():
    cfg = get_config()["economy"]
    url = f"https://{cfg['api_url']}{cfg['currency_endpoint']}"
    return _fetch_or_cache(cfg["currency_endpoint"], url)
