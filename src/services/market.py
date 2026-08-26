"""BIST piyasa durumu servisi.

Eski ``quote.py`` icindeki ``get_market_status()`` mantigi buraya tasindi ve
resmi tatil takvimi ile genisletildi. Sync fonksiyonlar saf (yan etkisiz);
endpoint yaniti icin ``get_market_status_payload()`` Redis'te 60s TTL ile
onbelleklenir (Redis proxy down ise her cagri dogrudan hesaplanir).
"""

import json
import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from src.core.redis import r

logger = logging.getLogger(__name__)

MARKET_TIMEZONE = ZoneInfo("Europe/Istanbul")
MARKET_OPEN = time(10, 0)
MARKET_CLOSE = time(18, 10)

# 2026 Turkiye resmi tatilleri (BIST islem gunu degil).
# NOT (v1): Yarim gunler (Kurban Arefesi 2026-05-26 ve Cumhuriyet Bayrami
# arifesi 2026-10-28) TAM GUN sayilir — borsa o gunlerde yarim seans aciktir
# ancak bilincli basitlestirme ile kapali kabul edilir.
TR_HOLIDAYS_2026: dict[date, str] = {
    date(2026, 1, 1): "Yılbaşı",
    date(2026, 4, 23): "Ulusal Egemenlik ve Çocuk Bayramı",
    date(2026, 5, 1): "Emek ve Dayanışma Günü",
    date(2026, 5, 19): "Atatürk'ü Anma, Gençlik ve Spor Bayramı",
    date(2026, 5, 26): "Kurban Bayramı Arefesi (yarım gün — v1'de tam gün)",
    date(2026, 5, 27): "Kurban Bayramı 1. Gün",
    date(2026, 5, 28): "Kurban Bayramı 2. Gün",
    date(2026, 5, 29): "Kurban Bayramı 3. Gün",
    date(2026, 7, 15): "Demokrasi ve Milli Birlik Günü",
    date(2026, 8, 30): "Zafer Bayramı",
    date(2026, 10, 28): "Cumhuriyet Bayramı Arifesi (yarım gün — v1'de tam gün)",
    date(2026, 10, 29): "Cumhuriyet Bayramı",
}

_CACHE_KEY = "market:status"
_CACHE_TTL = 60


def is_holiday(day: date) -> str | None:
    """Verilen gun resmi tatil ise adini, degilse ``None`` doner."""
    return TR_HOLIDAYS_2026.get(day)


def get_market_status(now: datetime | None = None) -> str:
    """BIST acik mi? (hafta ici + resmi tatil degil + 10:00-18:10 Istanbul saati)."""
    current = (now or datetime.now(timezone.utc)).astimezone(MARKET_TIMEZONE)
    if current.weekday() >= 5:
        return "closed"
    if is_holiday(current.date()):
        return "closed"
    return "open" if MARKET_OPEN <= current.time() < MARKET_CLOSE else "closed"


def next_open_at(now: datetime | None = None) -> datetime | None:
    """Bir sonraki acilis anini Istanbul saat diliminde doner.

    Piyasa su an aciksa ``None`` (zaten acik). En fazla 14 gun ileriye bakar.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(MARKET_TIMEZONE)
    if get_market_status(current) == "open":
        return None
    candidate = current.date()
    for _ in range(14):
        if candidate.weekday() < 5 and is_holiday(candidate) is None:
            open_dt = datetime.combine(candidate, MARKET_OPEN, tzinfo=MARKET_TIMEZONE)
            if open_dt > current:
                return open_dt
        candidate += timedelta(days=1)
    return None


async def get_market_status_payload(now: datetime | None = None) -> dict:
    """Endpoint yaniti: Redis'te 60s onbellekli (proxy down-tolerant)."""
    try:
        cached = await r.get(_CACHE_KEY)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    current = (now or datetime.now(timezone.utc)).astimezone(MARKET_TIMEZONE)
    holiday_name = is_holiday(current.date())
    next_open = next_open_at(current)
    payload = {
        "open": get_market_status(current) == "open",
        "next_open_at": next_open.isoformat() if next_open else None,
        "timezone": str(MARKET_TIMEZONE),
        "is_holiday": holiday_name is not None,
        "holiday_name": holiday_name,
        "as_of": current.isoformat(),
    }

    try:
        await r.set(_CACHE_KEY, json.dumps(payload), ex=_CACHE_TTL)
    except Exception:
        pass
    return payload


# ----------------------------------------------------------------------
# Mum (candle) yazma katmani icin ortak yardimcilar
#
# D1/D2 duzeltmesi: yfinance halt edilmis sembollerde duz/hacimsiz "placeholder"
# mum uretiyor, ve on-demand (Ticker.history, tz-aware) ile cron (yf.download,
# naive) yollari ayni seans icin iki farkli ts konvansiyonu ((21:00Z eski,
# 00:00Z yeni) yaziyor. Bu dorduzu tum yazim yollari (price.py, cron/tasks.py,
# bulk.py, scripts/seed_prices.py) tarafindan kullanilir; imzalar sabittir.
# ----------------------------------------------------------------------

# Kanonik normalizasyon sadece gunluk-ve-uzeri araliklar icin tanimli;
# intraday (5m/30m/1h vb.) araliklar oldugu gibi birakilir.
DAILY_INTERVALS = {"1d", "1wk", "1mo"}


def session_date(ts: datetime) -> date:
    """Bir mum zaman damgasini BIST seans tarihine cevirir (Istanbul saati).

    ``ts`` tz-aware olmalidir (DB'den TIMESTAMPTZ olarak okunan degerler
    hep boyledir). Hem eski (21:00Z gece yarisi) hem yeni (00:00Z gece
    yarisi) yazim konvansiyonu icin dogru seans tarihini dondurur, cunku
    ikisi de Istanbul saatine cevrildiginde ayni takvim gunune duser.
    """
    return ts.astimezone(MARKET_TIMEZONE).date()


def session_ts(d: date) -> datetime:
    """Bir seans tarihini kanonik saklama anina cevirir: Istanbul gece yarisi.

    Turkiye 2016'dan beri yaz saati uygulamiyor (sabit UTC+3), bu yuzden bu
    deger her zaman onceki UTC gununun 21:00'ine esittir.
    """
    return datetime(d.year, d.month, d.day, tzinfo=MARKET_TIMEZONE)


def normalize_candle_ts(ts: datetime, interval: str) -> datetime:
    """Gunluk-ve-uzeri mum zaman damgasini kanonik Istanbul gece yarisina
    normalize eder; intraday araliklari oldugu gibi birakir.

    Bu, D2'nin duzeltmesidir: on-demand yol (``Ticker.history``, tz-aware,
    21:00Z yazar) ile cron yolu (``yf.download``, naive, 00:00Z yazar) ayni
    seans icin farkli ``ts`` uretiyordu ve PK ``(ticker, interval, ts)``
    ON CONFLICT hicbir zaman tetiklenmiyordu — iki satir da kaliciydi.

    Naive (tz bilgisi olmayan) bir zaman damgasi ozellikle ``yf.download``'in
    cron yolundan gelir; bu deger UTC DEGIL, doganidan BIST seans tarihinin
    kendisidir (saat kismi her zaman 00:00) — bu yuzden UTC varsayip
    Istanbul'a cevirmek yerine dogrudan tarih olarak okunur. Bunu yanlis
    yapmak D2 hatasini geri getirir.
    """
    if interval not in DAILY_INTERVALS:
        return ts
    d = ts.date() if ts.tzinfo is None else session_date(ts)
    return session_ts(d)


def is_placeholder_candle(open_, high, low, close, volume) -> bool:
    """Halt edilmis bir sembolde yfinance'in urettigi duz/hacimsiz yer
    tutucu mumu tespit eder: hacim 0/None VE OHLC'nin dordu de esit.

    Herhangi bir fiyat ``None`` ise placeholder SAYILMAZ (bu durum ayri bir
    kontrolle -- Open/Close None kontrolu -- zaten elenir). Float esitligi
    icin ``math.isclose`` kullanilir; yfinance'in urettigi placeholder
    satirlarda dort deger de ayni kaynaktan (son islem fiyati) geldigi icin
    bu tam esitlestirmeyi de kapsar.
    """
    if open_ is None or high is None or low is None or close is None:
        return False
    if volume not in (0, None):
        return False
    return (
        math.isclose(open_, high)
        and math.isclose(open_, low)
        and math.isclose(open_, close)
    )
