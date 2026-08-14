import json
from datetime import date, datetime, timezone

from src.core.database import db
from src.core.redis import r
from src.services.market import MARKET_TIMEZONE, get_market_status

INTRADAY_INTERVALS = ("5m", "30m", "1h")


def _as_float(value) -> float | None:
    return float(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _session_date(ts: datetime) -> date:
    """Yfinance mum zaman damgasini BIST seans tarihine cevirir (TRT)."""
    return ts.astimezone(MARKET_TIMEZONE).date()


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
    """TAMAMLANMIS seanslarin gunluk mumlari (tarihe gore artan)."""
    by_date: dict[date, dict] = {}
    for row in daily:  # ts DESC gelir
        d = _session_date(row["ts"])
        by_date.setdefault(d, row)

    result = []
    for d in sorted(by_date):
        if d < today or (d == today and status == "closed"):
            result.append(by_date[d])
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

    if have_live:
        price = _as_float(intraday[0]["close"])
        previous_close = _as_float(last_session["close"]) if last_session else None
        as_of = intraday[0]["ts"]
        previous_close_ts = last_session["ts"] if last_session else None
    else:
        price = _as_float(last_session["close"]) if last_session else None
        previous_close = _as_float(prev_session["close"]) if prev_session else None
        as_of = last_session["ts"] if last_session else None
        previous_close_ts = prev_session["ts"] if prev_session else None

    # Mum verisi eksikse profil fallback'i
    if price is None:
        price = _as_float(market.get("currentPrice"))
    if previous_close is None:
        previous_close = _as_float(market.get("previousClose"))
    if as_of is None and market.get("regularMarketTime"):
        as_of = datetime.fromtimestamp(market["regularMarketTime"], tz=timezone.utc)
    if previous_close_ts is None and market.get("regularMarketTime"):
        previous_close_ts = datetime.fromtimestamp(market["regularMarketTime"], tz=timezone.utc)

    change = price - previous_close if price is not None and previous_close is not None else None
    # Acik piyasada canli intraday veri yoksa yaniltici 0.00 gostermeyelim.
    if status == "open" and not intraday:
        change_pct = None
    else:
        change_pct = change / previous_close * 100 if change is not None and previous_close else None

    stale = False
    if as_of is not None:
        age_seconds = (now_utc - as_of).total_seconds()
        stale = age_seconds > (20 * 60 if status == "open" else 3 * 86400)

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
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            f"""
            SELECT ticker, interval, ts, close
            FROM price_candles
            WHERE ticker IN ({placeholders})
              AND interval IN ('5m', '30m', '1h', '1d')
              AND close IS NOT NULL
            ORDER BY ticker, interval, ts DESC
            """,
            ticker_values,
        )
        rows = await cur.fetchall()

    # DB sorgusu bitti: mumlar bellekten islenirken baglanti iade edilsin
    # (redis/yfinance beklemesi sirasinda checked-out kalmasin).
    await db.release_current()

    grouped: dict[str, list[dict]] = {}
    for ticker, interval, ts, close in rows:
        key = ticker.removesuffix(".IS")
        interval_rows = [row for row in grouped.setdefault(key, []) if row["interval"] == interval]
        if len(interval_rows) < 2:
            grouped[key].append({"interval": interval, "ts": ts, "close": close})

    return {ticker: await _build_quote(ticker, grouped.get(ticker, [])) for ticker in normalized}


async def get_quote(ticker: str) -> dict:
    return (await get_quotes([ticker]))[ticker.upper().removesuffix(".IS")]
