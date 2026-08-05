import json
from datetime import datetime, time, timezone
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


def _session_start_utc(now: datetime | None = None) -> datetime:
    """Bugunku BIST seansinin baslangicini (10:00 TRT) UTC olarak dondurur."""
    current = (now or datetime.now(timezone.utc)).astimezone(MARKET_TIMEZONE)
    session_start_local = datetime.combine(current.date(), MARKET_OPEN, tzinfo=MARKET_TIMEZONE)
    return session_start_local.astimezone(timezone.utc)


def _read_cached_profile(ticker: str) -> dict:
    """Profil verisini Redis'ten okur. Okunamazsa bos dict doner.

    Summary ve price/current her ikisi de bu fonksiyonu kullanir; boylece
    iki endpoint ayni `previousClose` degerini gorur ve tutarsizlik olusmaz.
    """
    key = f"{ticker.upper().removesuffix('.IS')}.IS"
    try:
        cached = r.get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    return {}


def _build_quote(ticker: str, rows: list[dict]) -> dict:
    by_interval: dict[str, list[dict]] = {}
    for row in rows:
        by_interval.setdefault(row["interval"], []).append(row)

    daily = by_interval.get("1d", [])
    intraday = next((by_interval.get(interval, []) for interval in INTRADAY_INTERVALS if by_interval.get(interval)), [])
    status = get_market_status()
    session_start = _session_start_utc()

    if status == "open" and intraday:
        current_row = intraday[0]
    else:
        current_row = daily[0] if daily else (intraday[0] if intraday else None)

    # Son TAMAMLANMIS seansin günlük mumu. Bugunku (henuz kapanmamis)
    # mum asla referans olarak kullanilmaz.
    completed_daily = [row for row in daily if row["ts"] < session_start]
    previous_row = completed_daily[0] if completed_daily else (daily[1] if len(daily) > 1 else None)

    profile = _read_cached_profile(ticker)
    market = profile.get("market", {}) or {}

    price = _as_float(current_row["close"]) if current_row else _as_float(market.get("currentPrice"))

    # previous_close birincil kaynak: profil (yFinance previousClose).
    # Bos ise son tamamlanmis seansin mum kapanisi kullanilir.
    previous_close = _as_float(market.get("previousClose"))
    previous_close_ts: datetime | None = None
    if previous_close is None and previous_row is not None:
        previous_close = _as_float(previous_row["close"])
        previous_close_ts = previous_row["ts"]
    elif previous_close is not None and market.get("regularMarketTime"):
        previous_close_ts = datetime.fromtimestamp(market["regularMarketTime"], tz=timezone.utc)

    if status == "open":
        try:
            cached_live_price = r.get(f"latest_price:{ticker.upper().removesuffix('.IS')}.IS:5m")
            if cached_live_price is not None:
                price = float(cached_live_price)
        except (TypeError, ValueError):
            pass

    as_of = current_row["ts"] if current_row else None
    if as_of is None and market.get("regularMarketTime"):
        as_of = datetime.fromtimestamp(market["regularMarketTime"], tz=timezone.utc)

    change = price - previous_close if price is not None and previous_close is not None else None
    change_pct = change / previous_close * 100 if change is not None and previous_close else None
    stale = False
    if as_of is not None:
        age_seconds = (datetime.now(timezone.utc) - as_of).total_seconds()
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
        "change_window": "previous_session_close",
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
