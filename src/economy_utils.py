import http.client
from src.config import get_config
from src.redis_connection import r
from dotenv import load_dotenv
import os
import json
import requests

load_dotenv()
api_key = os.getenv("COLLECT_API_KEY")


def _cache_key(endpoint: str) -> str:
    return f"economy:{endpoint.strip('/').replace('/', '_')}"


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


def _send_request(url: str):
    headers = {
        'content-type': "application/json",
        'authorization': f"apikey {api_key}"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()["result"]
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}


def get_gold_prices():
    url = f"https://{get_config()['economy']['api_url']}{get_config()['economy']['gold_endpoint']}"
    return _fetch_or_cache(get_config()['economy']['gold_endpoint'], url)


def get_silver_prices():
    url = f"https://{get_config()['economy']['api_url']}{get_config()['economy']['silver_endpoint']}"
    return _fetch_or_cache(get_config()['economy']['silver_endpoint'], url)


def get_currency_symbols():
    url = f"https://{get_config()['economy']['api_url']}{get_config()['economy']['currency_symbols_endpoint']}"
    return _fetch_or_cache(get_config()['economy']['currency_symbols_endpoint'], url)


def get_currency():
    url = f"https://{get_config()['economy']['api_url']}{get_config()['economy']['currency_endpoint']}"
    return _fetch_or_cache(get_config()['economy']['currency_endpoint'], url)
