import json
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from src.core.database import db
from src.core.redis import r


MARKET_TIMEZONE = ZoneInfo("Europe/Istanbul")
MARKET_OPEN = time(10, 0)
MARKET_CLOSE = time(18, 10)
INTRADAY_INTERVALS = ("5m", "30m", "1h")


def get_market_status(now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(MARKET_TIMEZONE)
    if current.weekday() >= 5:
        return "closed"
    return "open" if MARKET_OPEN <= current.time() < MARKET_CLOSE else "closed"


def _as_float(value) -> float | None:
    return float(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _session_date(ts: datetime) -> date:
    """Yfinance mum zaman damgasini BIST seans tarihine cevirir (TRT)."""
    return ts.astimezone(MARKET_TIMEZONE).date()


def _read_cached_profile(ticker: str) -> dict:
    """Profil verisini Redis'ten okur. Okunamazsa bos dict doner.

    Summary ve price/current her ikisi de bu fonksiyonu kullanir; boylece
    iki endpoint ayni veriyi gorur ve tutarsizlik olusmaz.
    """
    key = f"{ticker.upper().removesuffix('.IS')}.IS"
    try:
        cached = r.get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    return {}


def _completed_daily(daily: list[dict], today: date, status: str) -> list[dict]:
    """TAMAMLANMIS seanslarin gunluk mumlari (tarihe gore artan).

    Ayni seans gunune denk gelen birden fazla mum varsa en yenisi alinir.
    - Piyasa acikken bugunku seans dahil edilmez.
    - Piyasa kapaliyken (kapanis sonrasi / pre-market / hafta sonu) bugunku
      seans varsa dahil edilir; boylece kapanis sonrasi bugunun degisimi,
      pre-market'te ise son tamamlanan gunun degisimi gosterilir.
    """
    by_date: dict[date, dict] = {}
    for row in daily:  # ts DESC gelir
        d = _session_date(row["ts"])
        by_date.setdefault(d, row)

    result = []
    for d in sorted(by_date):
        if d < today or (d == today and status == "closed"):
            result.append(by_date[d])
    return result


def _build_quote(ticker: str, rows: list[dict]) -> dict:
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

    profile = _read_cached_profile(ticker)
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
    # Acik piyasada canli intraday veri yoksa yaniltici 0.00 gostermeyelim;
    # pre-market/kapanis sonrasinda son tamamlanan seansin degisimi gosterilir.
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


def get_quotes(tickers: list[str]) -> dict[str, dict]:
    normalized = [ticker.upper().removesuffix(".IS") for ticker in tickers]
    ticker_values = [f"{ticker}.IS" for ticker in normalized]
    if not ticker_values:
        return {}

    placeholders = ",".join(["%s"] * len(ticker_values))
    with db.cursor() as cur:
        cur.execute(
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
        rows = cur.fetchall()

    grouped: dict[str, list[dict]] = {}
    for ticker, interval, ts, close in rows:
        key = ticker.removesuffix(".IS")
        interval_rows = [row for row in grouped.setdefault(key, []) if row["interval"] == interval]
        if len(interval_rows) < 2:
            grouped[key].append({"interval": interval, "ts": ts, "close": close})

    return {ticker: _build_quote(ticker, grouped.get(ticker, [])) for ticker in normalized}


def get_quote(ticker: str) -> dict:
    return get_quotes([ticker])[ticker.upper().removesuffix(".IS")]
