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

# GenelPara list adlari
_GENELPARA_DOVIZ = "doviz"
_GENELPARA_ALTIN = "altin"
_GENELPARA_EMTIA = "emtia"

# GenelPara sembolu -> truncgil-stili anahtar.
# API cevap sozlesmesi (anahtarlar) korunur; portfoy metal pozisyonlari bozulmaz.
GENELPARA_GOLD_MAP = {
    "GA": "gram-altin",
    "C": "ceyrek-altin",
    "XAUUSD": "ons",
    "XHGLD": "gram-has-altin",
    "Y": "yarim-altin",
    "T": "tam-altin",
    "CMR": "cumhuriyet-altini",
    "ATA": "ata-altin",
    "14": "14-ayar-altin",
    "18": "18-ayar-altin",
    "22": "22-ayar-bilezik",
    "IKB": "ikibucuk-altin",
    "BSL": "besli-altin",
    "GR": "gremse-altin",
    "RA": "resat-altin",
    "HA": "hamit-altin",
}

# Doviz listesinden cikarilacaklar (TRY baz kur; kart olarak gosterilmek istenmez)
_CURRENCY_EXCLUDED = {"TRY"}


def _genelpara_url(list_name: str, symbols: str | None = None) -> str:
    base = get_config()["economy"]["api_url"].rstrip("/")
    url = f"{base}?list={list_name}"
    if symbols:
        url += f"&sembol={symbols}"
    return url


def _to_tr_string(value: str | None) -> str | None:
    """GenelPara nokta ondaligini virgul ondaliga cevirir (frontend beklentisi)."""
    if value is None:
        return None
    return str(value).replace(".", ",")


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _map_entry(item, type_label: str) -> dict | None:
    """GenelPara item'ini truncgil-stili {Buying, Selling, Change, Type} yapar."""
    if not isinstance(item, dict):
        return None
    alis = _as_float(item.get("alis"))
    if alis is None:
        return None

    satis = _as_float(item.get("satis"))
    degisim = _as_float(item.get("degisim"))
    change_pct = (degisim / alis) * 100 if (degisim is not None and alis > 0) else None

    return {
        "Buying": _to_tr_string(item.get("alis")),
        "Selling": _to_tr_string(item.get("satis")),
        "Change": f"%{change_pct:.2f}" if change_pct is not None else "0",
        "Type": type_label,
    }


def _fetch_genelpara(list_name: str, symbols: str | None = None) -> dict:
    """GenelPara listesini getirir; Redis cache'li.

    Basarisiz/veri yoksa {} doner (cagiran DB fallback'ine duser).
    """
    cache_key = f"genelpara:{list_name}" + (f":{symbols}" if symbols else "")
    cached = r.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    try:
        response = requests.get(
            _genelpara_url(list_name, symbols),
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("GenelPara %s istegi basarisiz: %s", list_name, exc)
        return {}

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}

    r.set(cache_key, json.dumps(data, ensure_ascii=False), ex=get_config()["economy"]["cache_ttl"])
    return data


def _latest_market_rates(data_type: str) -> dict:
    """market_rates tablosundaki en son kayitli (saglikli) veriyi dondurur."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT data FROM market_rates WHERE data_type = %s ORDER BY id DESC LIMIT 1",
                (data_type,),
            )
            row = cur.fetchone()
        if row and row[0]:
            parsed = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    return {}


def _cache_result(key: str, data: dict) -> None:
    """Sonucu cache'ler. Bos/hata durumunda kisa TTL kullanir (saglayiciyi domme)."""
    ttl = get_config()["economy"]["cache_ttl"] if data and "error" not in data else 60
    r.set(key, json.dumps(data or {}, ensure_ascii=False), ex=ttl)


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

    raw = _fetch_genelpara(_GENELPARA_ALTIN)
    gold_prices = {}
    for gp_symbol, key in GENELPARA_GOLD_MAP.items():
        entry = _map_entry(raw.get(gp_symbol), "Gold")
        if entry:
            gold_prices[key] = entry

    if not gold_prices:
        gold_prices = _latest_market_rates("gold")

    _cache_result("gold_prices", gold_prices)
    _persist_market_data("gold", gold_prices)
    _persist_economy_rates(gold_prices)
    return gold_prices


def get_silver_price():
    cached = r.get("silver_price")
    if cached:
        return json.loads(cached)

    raw = _fetch_genelpara(_GENELPARA_ALTIN)
    entry = _map_entry(raw.get("GAG"), "Gold")
    silver_price = {"gumus": entry} if entry else {}

    if not silver_price:
        silver_price = _latest_market_rates("silver")

    _cache_result("silver_price", silver_price)
    _persist_market_data("silver", silver_price)
    _persist_economy_rates(silver_price)
    return silver_price


def get_gram_platinum_price():
    cached = r.get("gram_platinum_price")
    if cached:
        return json.loads(cached)

    raw = _fetch_genelpara(_GENELPARA_EMTIA, "XPTUSD")
    entry = _map_entry(raw.get("XPTUSD"), "Commodity")
    gram_platinum_price = {"gram-platin": entry} if entry else {}

    if not gram_platinum_price:
        gram_platinum_price = _latest_market_rates("platinum")

    _cache_result("gram_platinum_price", gram_platinum_price)
    _persist_market_data("platinum", gram_platinum_price)
    _persist_economy_rates(gram_platinum_price)
    return gram_platinum_price


def get_gram_palladium_price():
    cached = r.get("gram_palladium_price")
    if cached:
        return json.loads(cached)

    raw = _fetch_genelpara(_GENELPARA_EMTIA, "XPDUSD")
    entry = _map_entry(raw.get("XPDUSD"), "Commodity")
    gram_palladium_price = {"gram-paladyum": entry} if entry else {}

    if not gram_palladium_price:
        gram_palladium_price = _latest_market_rates("palladium")

    _cache_result("gram_palladium_price", gram_palladium_price)
    _persist_market_data("palladium", gram_palladium_price)
    _persist_economy_rates(gram_palladium_price)
    return gram_palladium_price


def get_currency():
    cached = r.get("currency")
    if cached:
        return json.loads(cached)

    raw = _fetch_genelpara(_GENELPARA_DOVIZ)
    currency = {}
    for code, item in raw.items():
        if code in _CURRENCY_EXCLUDED:
            continue
        entry = _map_entry(item, "Currency")
        if entry:
            currency[code] = entry

    if not currency:
        currency = _latest_market_rates("currency")

    _cache_result("currency", currency)
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
