import asyncio
import json
from datetime import date, datetime, timedelta, timezone

from src.core.database import db
from src.core.redis import r
from src.services.market import (
    MARKET_TIMEZONE,
    expected_last_session_date,
    get_market_status,
    last_trading_day,
    session_date,
    session_ts,
)

INTRADAY_INTERVALS = ("5m", "30m", "1h")

# Ayni seans icin iki yazim konvansiyonundan (ham UTC vs Istanbul gece
# yarisi) mukerrer satir gelebilir; dedup icin aralik basina en fazla bu
# kadar satir tutulur (kucuk kalsin).
_MAX_ROWS_PER_INTERVAL = 6

# Bu sayiya kadar ticker iceren isteklerde eksik gunluk kapanislar aninda
# telafi edilir; daha buyuk (market listesi) isteklerde ag cagrisi YAPILMAZ,
# telafi turunu 30 dk'lik daily_close_repair cron'u ustlenir.
_ENSURE_FRESH_MAX_TICKERS = 25


def _as_float(value) -> float | None:
    return float(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _read_cached_profile(ticker: str) -> dict:
    """Profil verisini Redis'ten okur. Okunamazsa bos dict doner."""
    key = f"{ticker.upper().removesuffix('.IS')}.IS"
    try:
        cached = await r.get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    return {}


def _completed_daily(daily: list[dict], today: date, status: str) -> list[dict]:
    """TAMAMLANMIS seanslarin gunluk mumlari (tarihe gore artan).

    Ayni seansa ait mukerrer satirlar tarihe gore tekillestirilir: kanonik
    ``session_ts(session_date)`` damgasina sahip satir varsa o tercih edilir,
    yoksa en yeni ``ts`` (satirlar ``ts`` DESC geldigi icin ilk gorulen)
    kazanir.
    """
    best: dict[date, dict] = {}
    for row in daily:  # ts DESC gelir
        d = session_date(row["ts"])
        current = best.get(d)
        if current is None:
            best[d] = row
        elif row["ts"] == session_ts(d):
            # Kanonik damga her zaman kazanir.
            best[d] = row

    result = []
    for d in sorted(best):
        if d < today or (d == today and status == "closed"):
            result.append(best[d])
    return result


async def _build_quote(ticker: str, rows: list[dict]) -> dict:
    by_interval: dict[str, list[dict]] = {}
    for row in rows:
        by_interval.setdefault(row["interval"], []).append(row)

    daily = by_interval.get("1d", [])
    intraday = next((by_interval.get(interval, []) for interval in INTRADAY_INTERVALS if by_interval.get(interval)), [])
    status = get_market_status()
    now_utc = datetime.now(timezone.utc)
    today = now_utc.astimezone(MARKET_TIMEZONE).date()

    completed = _completed_daily(daily, today, status)
    last_session = completed[-1] if completed else None
    prev_session = completed[-2] if len(completed) >= 2 else None

    profile = await _read_cached_profile(ticker)
    market = profile.get("market", {}) or {}

    have_live = status == "open" and bool(intraday)

    price = None
    previous_close = None
    as_of = None
    previous_close_ts = None

    if have_live:
        price = _as_float(intraday[0]["close"])
        as_of = intraday[0]["ts"]
        # Onceki kapanis yalnizca hemen onceki islem seansina aitse gecerli;
        # araya tatil/hafta sonu disinda bosluk girerse karistirmayalim.
        if last_session is not None and session_date(last_session["ts"]) == last_trading_day(today):
            previous_close = _as_float(last_session["close"])
            previous_close_ts = last_session["ts"]
    elif last_session is not None:
        price = _as_float(last_session["close"])
        as_of = last_session["ts"]
        # Onceki seans, son seansin hemen onceki islem gunu degilse
        # (veri boslugu) yaniltici degisim uretmemek icin None birakilir.
        if prev_session is not None and session_date(prev_session["ts"]) == last_trading_day(session_date(last_session["ts"])):
            previous_close = _as_float(prev_session["close"])
            previous_close_ts = prev_session["ts"]

    # Profil fallback'i YALNIZCA hicbir DB fiyati yoksa ve cift halinde
    # kullanilir; DB fiyati ile profil previousClose'u asla karistirilmaz.
    if price is None and previous_close is None:
        price = _as_float(market.get("currentPrice"))
        previous_close = _as_float(market.get("previousClose"))
        if market.get("regularMarketTime"):
            as_of = datetime.fromtimestamp(market["regularMarketTime"], tz=timezone.utc)
            previous_close_ts = as_of

    change = price - previous_close if price is not None and previous_close is not None else None
    # Acik piyasada canli intraday veri yoksa yaniltici 0.00 gostermeyelim.
    if status == "open" and not intraday:
        change_pct = None
    else:
        change_pct = change / previous_close * 100 if change is not None and previous_close else None

    # Acik: 20 dakikadan eski as_of bayat. Kapali: son tamamlanmis seans,
    # beklenen seans tarihinden eskiyse (veya hic seans yoksa) bayat.
    if status == "open":
        stale = as_of is None or (now_utc - as_of) > timedelta(minutes=20)
    else:
        stale = last_session is None or session_date(last_session["ts"]) < expected_last_session_date(now_utc)

    return {
        "ticker": ticker,
        "price": price,
        "previous_close": previous_close,
        "absolute_change": round(change, 4) if change is not None else None,
        "change_pct": round(change_pct, 4) if change_pct is not None else None,
        "as_of": _iso(as_of),
        "previous_close_as_of": _iso(previous_close_ts),
        "market_status": status,
        "is_stale": stale,
        "change_window": "last_session_change" if status == "closed" else "previous_session_close",
    }


async def get_quotes(tickers: list[str]) -> dict[str, dict]:
    normalized = [ticker.upper().removesuffix(".IS") for ticker in tickers]
    ticker_values = [f"{ticker}.IS" for ticker in normalized]
    if not ticker_values:
        return {}

    # price_candles tablosunun varligini garanti et (init_db calismamis
    # ortamlarda da SELECT patlamasin; DDL idempotent).
    from src.services.price import _init_db
    await _init_db()

    placeholders = ",".join(["%s"] * len(ticker_values))
    # Tum gecmisi taramayi onle: 1d icin 45 gun, intraday icin 2 gun yeter.
    now_utc = datetime.now(timezone.utc)
    daily_cutoff = now_utc - timedelta(days=45)
    intraday_cutoff = now_utc - timedelta(days=2)
    params = [*ticker_values, daily_cutoff, intraday_cutoff]
    query = f"""
            SELECT ticker, interval, ts, close
            FROM price_candles
            WHERE ticker IN ({placeholders})
              AND (
                    (interval = '1d' AND ts >= %s)
                    OR (interval IN ('5m', '30m', '1h') AND ts >= %s)
                  )
              AND close IS NOT NULL
            ORDER BY ticker, interval, ts DESC
            """

    async def _read_grouped() -> dict[str, list[dict]]:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(query, params)
            fetched = await cur.fetchall()
        # DB sorgusu bitti: mumlar bellekten islenirken baglanti iade edilsin
        # (redis/yfinance beklemesi sirasinda checked-out kalmasin).
        await db.release_current()

        grouped: dict[str, list[dict]] = {}
        for ticker, interval, ts, close in fetched:
            key = ticker.removesuffix(".IS")
            interval_rows = [row for row in grouped.setdefault(key, []) if row["interval"] == interval]
            if len(interval_rows) < _MAX_ROWS_PER_INTERVAL:
                grouped[key].append({"interval": interval, "ts": ts, "close": close})
        return grouped

    grouped = await _read_grouped()

    # Kucuk isteklerde (detay/ozet ekrani) gunluk kapanisi eksik kalan
    # ticker'lari tek seferlik telafi et: 18:35 daily_close Yahoo bari
    # gelmeden kosmussa mum 24 saat yazilmamis kalir ve ozet bayat gorunur.
    # Buyuk listelerde (market) ag cagrisi YAPILMAZ; daily_close_repair
    # cron'u kapsar.
    if len(normalized) <= _ENSURE_FRESH_MAX_TICKERS:
        expected = expected_last_session_date(now_utc)
        behind = []
        for ticker in normalized:
            latest_daily = next(
                (row for row in grouped.get(ticker, []) if row["interval"] == "1d"),
                None,
            )
            if latest_daily is None or session_date(latest_daily["ts"]) < expected:
                behind.append(ticker)
        if behind:
            from src.services.price import ensure_recent_daily_candle

            sem = asyncio.Semaphore(4)

            async def _ensure(base: str) -> bool:
                async with sem:
                    return await ensure_recent_daily_candle(base)

            filled = await asyncio.gather(*(_ensure(t) for t in behind))
            if any(filled):
                grouped = await _read_grouped()

    return {ticker: await _build_quote(ticker, grouped.get(ticker, [])) for ticker in normalized}


async def get_quote(ticker: str) -> dict:
    return (await get_quotes([ticker]))[ticker.upper().removesuffix(".IS")]
