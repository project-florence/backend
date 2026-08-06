import logging
from src.core.config import get_config
from src.core.redis import r
from src.core.database import db
from datetime import datetime
import psycopg2.extras
from dotenv import load_dotenv
import os
import json
import requests

logger = logging.getLogger(__name__)

load_dotenv()

EXCLUDED_FIELDS = ["Update_Date", "ons", "gram-altin", "gram-has-altin", "ceyrek-altin",
                   "yarim-altin", "tam-altin", "cumhuriyet-altini",
                   "ata-altin", "14-ayar-altin", "18-ayar-altin",
                   "ikibucuk-altin", "altin", "gremse-altin", "22-ayar-bilezik", "besli-altin",
                   "resat-altin", "hamit-altin", "gumus", "gram-platin",
                   "gram-paladyum"]

def _get_specific_fields(data, fields):
    if not isinstance(data, dict):
        return {}

    return {field: data[field] for field in fields if field in data}


def _get_all_except(data, excluded_fields):
    if not isinstance(data, dict):
        return {}

    return {field: data[field] for field in data if field not in excluded_fields}


def _send_request(url: str):
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}


def _persist_market_data(data_type: str, data: dict) -> None:
    if not data or "error" in data:
        return
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO market_rates (data_type, data) VALUES (%s, %s)",
                (data_type, json.dumps(data, ensure_ascii=False))
            )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("market_rates persist basarisiz: %s", data_type, exc_info=True)


def _persist_economy_rates(data: dict) -> None:
    if not data or "error" in data:
        return
    try:
        with db.cursor() as cur:
            args = []
            for ticker, price in data.items():
                if isinstance(price, dict):
                    price = json.dumps(price, ensure_ascii=False)
                args.append((ticker, price))
            cur.executemany(
                "INSERT INTO economy_rates (ticker, ts, price) VALUES (%s, NOW(), %s) ON CONFLICT DO NOTHING",
                args,
            )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("economy_rates persist basarisiz: %s", list(data)[:3], exc_info=True)


def get_gold_prices():
    cached = r.get("gold_prices")
    if cached:
        return json.loads(cached)

    j = _send_request((get_config()["economy"]["api_url"]))
    gold_prices = _get_specific_fields(j, ["ons", "gram-altin", "gram-has-altin",
                                           "ceyrek-altin", "yarim-altin", "tam-altin",
                                           "cumhuriyet-altini", "ata-altin", "14-ayar-altin",
                                           "18-ayar-altin", "ikibucuk-altin", "altin", "22-ayar-bilezik", "besli-altin",
                                           "gremse-altin", "resat-altin", "hamit-altin"])

    r.set("gold_prices", json.dumps(gold_prices, ensure_ascii=False), ex=get_config()["economy"]["cache_ttl"])
    _persist_market_data("gold", gold_prices)
    _persist_economy_rates(gold_prices)
    return gold_prices


def get_silver_price():
    cached = r.get("silver_price")
    if cached:
        return json.loads(cached)

    j = _send_request((get_config()["economy"]["api_url"]))
    silver_price = _get_specific_fields(j, ["gumus"])

    r.set("silver_price", json.dumps(silver_price, ensure_ascii=False), ex=get_config()["economy"]["cache_ttl"])
    _persist_market_data("silver", silver_price)
    _persist_economy_rates(silver_price)
    return silver_price

def get_gram_platinum_price():
    cached = r.get("gram_platinum_price")
    if cached:
        return json.loads(cached)

    j = _send_request((get_config()["economy"]["api_url"]))
    gram_platinum_price = _get_specific_fields(j, ["gram-platin"])

    r.set("gram_platinum_price", json.dumps(gram_platinum_price, ensure_ascii=False), ex=get_config()["economy"]["cache_ttl"])
    _persist_market_data("platinum", gram_platinum_price)
    _persist_economy_rates(gram_platinum_price)
    return gram_platinum_price

def get_gram_palladium_price():
    cached = r.get("gram_palladium_price")
    if cached:
        return json.loads(cached)

    j = _send_request((get_config()["economy"]["api_url"]))
    gram_palladium_price = _get_specific_fields(j, ["gram-paladyum"])

    r.set("gram_palladium_price", json.dumps(gram_palladium_price, ensure_ascii=False), ex=get_config()["economy"]["cache_ttl"])
    _persist_market_data("palladium", gram_palladium_price)
    _persist_economy_rates(gram_palladium_price)
    return gram_palladium_price


def get_currency():
    cached = r.get("currency")
    if cached:
        return json.loads(cached)

    j = _send_request(get_config()["economy"]["api_url"])
    currency = _get_all_except(j, EXCLUDED_FIELDS)

    r.set("currency", json.dumps(currency, ensure_ascii=False),
          ex=get_config()["economy"]["cache_ttl"])
    _persist_market_data("currency", currency)
    _persist_economy_rates(currency)
    return currency


def get_economy_rate_history(ticker: str, start: datetime, end: datetime) -> list[dict]:
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT ts, price FROM economy_rates "
            "WHERE ticker = %s AND ts >= %s AND ts <= %s ORDER BY ts",
            (ticker, start, end),
        )
        rows = cur.fetchall()
    return [{"ts": row["ts"].isoformat(), "price": row["price"]} for row in rows]