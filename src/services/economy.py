import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.clients.http import get_client
from src.core.config import get_config
from src.core.database import db
from src.core.redis import r

logger = logging.getLogger(__name__)

load_dotenv()

MARKET_TIMEZONE = ZoneInfo("Europe/Istanbul")

# GenelPara list adlari
_GENELPARA_DOVIZ = "doviz"
_GENELPARA_ALTIN = "altin"
_GENELPARA_EMTIA = "emtia"

# GenelPara sembolu -> truncgil-stili anahtar.
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


def _parse_number(value) -> float | None:
    """Virgul/nokta ondalikli sayi metnini float'a cevirir."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


async def _previous_close(ticker: str) -> float | None:
    """Ticker'in bugun baslamadan onceki son kayitli fiyatini dondurur."""
    try:
        now = datetime.now(timezone.utc)
        today_local = now.astimezone(MARKET_TIMEZONE).date()
        today_start_utc = datetime.combine(today_local, time.min, tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc)
        min_ts = today_start_utc - timedelta(days=3)
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT price FROM economy_rates WHERE ticker = %s AND ts >= %s AND ts < %s ORDER BY ts DESC LIMIT 1",
                (ticker, min_ts, today_start_utc),
            )
            row = await cur.fetchone()
        if row and row[0]:
            price = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            if isinstance(price, dict):
                return _parse_number(price.get("Buying"))
    except Exception:
        pass
    return None


def _map_entry(item, type_label: str, prev_close: float | None = None) -> dict | None:
    """GenelPara item'ini truncgil-stili {Buying, Selling, Change, Type} yapar."""
    if not isinstance(item, dict):
        return None
    alis = _as_float(item.get("alis"))
    if alis is None:
        return None

    satis = _as_float(item.get("satis"))
    change_pct = None
    if prev_close is not None and prev_close > 0:
        change_pct = (alis - prev_close) / prev_close * 100

    return {
        "Buying": _to_tr_string(item.get("alis")),
        "Selling": _to_tr_string(item.get("satis")),
        "Change": f"%{change_pct:.2f}" if change_pct is not None else None,
        "Type": type_label,
    }


async def _fetch_genelpara(list_name: str, symbols: str | None = None) -> dict:
    """GenelPara listesini getirir; Redis cache'li. Basarisizsa {} doner."""
    cache_key = f"genelpara:{list_name}" + (f":{symbols}" if symbols else "")
    cached = await r.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    try:
        client = await get_client()
        response = await client.get(
            _genelpara_url(list_name, symbols),
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("GenelPara %s istegi basarisiz: %s", list_name, exc)
        return {}

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}

    await r.set(cache_key, json.dumps(data, ensure_ascii=False), ex=get_config()["economy"]["cache_ttl"])
    return data


async def _latest_market_rates(data_type: str) -> dict:
    """market_rates tablosundaki en son kayitli (saglikli) veriyi dondurur."""
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT data FROM market_rates WHERE data_type = %s ORDER BY id DESC LIMIT 1",
                (data_type,),
            )
            row = await cur.fetchone()
        if row and row[0]:
            parsed = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    return {}


async def _cache_result(key: str, data: dict) -> None:
    """Sonucu cache'ler. Bos/hata durumunda kisa TTL kullanir."""
    ttl = get_config()["economy"]["cache_ttl"] if data and "error" not in data else 60
    await r.set(key, json.dumps(data or {}, ensure_ascii=False), ex=ttl)


async def _persist_market_data(data_type: str, data: dict) -> None:
    if not data or "error" in data:
        return
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "INSERT INTO market_rates (data_type, data) VALUES (%s, %s)",
                (data_type, json.dumps(data, ensure_ascii=False))
            )
            # Commit blok icinde (otomatik iade rollback etmesin).
            await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("market_rates persist basarisiz: %s", data_type, exc_info=True)


async def _persist_economy_rates(data: dict) -> None:
    if not data or "error" in data:
        return
    try:
        async with db.cursor(row_factory=None) as cur:
            args = []
            for ticker, price in data.items():
                if isinstance(price, dict):
                    price = json.dumps(price, ensure_ascii=False)
                args.append((ticker, price))
            await cur.executemany(
                "INSERT INTO economy_rates (ticker, ts, price) VALUES (%s, NOW(), %s) ON CONFLICT DO NOTHING",
                args,
            )
            # Commit blok icinde (otomatik iade rollback etmesin).
            await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("economy_rates persist basarisiz: %s", list(data)[:3], exc_info=True)


async def get_gold_prices():
    cached = await r.get("gold_prices")
    if cached:
        return json.loads(cached)

    raw = await _fetch_genelpara(_GENELPARA_ALTIN)
    gold_prices = {}
    for gp_symbol, key in GENELPARA_GOLD_MAP.items():
        entry = _map_entry(raw.get(gp_symbol), "Gold", await _previous_close(key))
        if entry:
            gold_prices[key] = entry

    if not gold_prices:
        gold_prices = await _latest_market_rates("gold")

    await _cache_result("gold_prices", gold_prices)
    await _persist_market_data("gold", gold_prices)
    await _persist_economy_rates(gold_prices)
    return gold_prices


async def get_silver_price():
    cached = await r.get("silver_price")
    if cached:
        return json.loads(cached)

    raw = await _fetch_genelpara(_GENELPARA_ALTIN)
    entry = _map_entry(raw.get("GAG"), "Gold", await _previous_close("gumus"))
    silver_price = {"gumus": entry} if entry else {}

    if not silver_price:
        silver_price = await _latest_market_rates("silver")

    await _cache_result("silver_price", silver_price)
    await _persist_market_data("silver", silver_price)
    await _persist_economy_rates(silver_price)
    return silver_price


async def get_gram_platinum_price():
    cached = await r.get("gram_platinum_price")
    if cached:
        return json.loads(cached)

    raw = await _fetch_genelpara(_GENELPARA_EMTIA, "XPTUSD")
    entry = _map_entry(raw.get("XPTUSD"), "Commodity", await _previous_close("gram-platin"))
    gram_platinum_price = {"gram-platin": entry} if entry else {}

    if not gram_platinum_price:
        gram_platinum_price = await _latest_market_rates("platinum")

    await _cache_result("gram_platinum_price", gram_platinum_price)
    await _persist_market_data("platinum", gram_platinum_price)
    await _persist_economy_rates(gram_platinum_price)
    return gram_platinum_price


async def get_gram_palladium_price():
    cached = await r.get("gram_palladium_price")
    if cached:
        return json.loads(cached)

    raw = await _fetch_genelpara(_GENELPARA_EMTIA, "XPDUSD")
    entry = _map_entry(raw.get("XPDUSD"), "Commodity", await _previous_close("gram-paladyum"))
    gram_palladium_price = {"gram-paladyum": entry} if entry else {}

    if not gram_palladium_price:
        gram_palladium_price = await _latest_market_rates("palladium")

    await _cache_result("gram_palladium_price", gram_palladium_price)
    await _persist_market_data("palladium", gram_palladium_price)
    await _persist_economy_rates(gram_palladium_price)
    return gram_palladium_price


async def get_currency():
    cached = await r.get("currency")
    if cached:
        return json.loads(cached)

    raw = await _fetch_genelpara(_GENELPARA_DOVIZ)
    currency = {}
    for code, item in raw.items():
        if code in _CURRENCY_EXCLUDED:
            continue
        entry = _map_entry(item, "Currency", await _previous_close(code))
        if entry:
            currency[code] = entry

    if not currency:
        currency = await _latest_market_rates("currency")

    await _cache_result("currency", currency)
    await _persist_market_data("currency", currency)
    await _persist_economy_rates(currency)
    return currency


async def get_economy_rate_history(ticker: str, start: datetime, end: datetime) -> list[dict]:
    async with db.cursor() as cur:
        await cur.execute(
            "SELECT ts, price FROM economy_rates "
            "WHERE ticker = %s AND ts >= %s AND ts <= %s ORDER BY ts",
            (ticker, start, end),
        )
        rows = await cur.fetchall()
    return [{"ts": row["ts"].isoformat(), "price": row["price"]} for row in rows]
