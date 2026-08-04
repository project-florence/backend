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


def _build_quote(ticker: str, rows: list[dict], fallback: dict | None = None) -> dict:
    by_interval: dict[str, list[dict]] = {}
    for row in rows:
        by_interval.setdefault(row["interval"], []).append(row)

    daily = by_interval.get("1d", [])
    intraday = next((by_interval.get(interval, []) for interval in INTRADAY_INTERVALS if by_interval.get(interval)), [])
    status = get_market_status()

    if status == "open" and intraday:
        current_row = intraday[0]
        previous_row = daily[0] if daily else None
    else:
        current_row = daily[0] if daily else (intraday[0] if intraday else None)
        previous_row = daily[1] if len(daily) > 1 else None

    market = fallback or {}
    price = _as_float(current_row["close"]) if current_row else _as_float(market.get("currentPrice"))
    previous_close = _as_float(market.get("previousClose"))
    if previous_close is None:
        previous_close = _as_float(previous_row["close"]) if previous_row else None
    if status == "open":
        try:
            cached_live_price = r.get(f"latest_price:{ticker}.IS:5m")
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
        "previous_close_as_of": _iso(previous_row["ts"] if previous_row else None),
        "market_status": status,
        "is_stale": stale,
        "change_window": "previous_session_close",
    }


def get_quotes(tickers: list[str], fallbacks: dict[str, dict] | None = None) -> dict[str, dict]:
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

    return {
        ticker: _build_quote(ticker, grouped.get(ticker, []), (fallbacks or {}).get(ticker))
        for ticker in normalized
    }


def get_quote(ticker: str, fallback: dict | None = None) -> dict:
    return get_quotes([ticker], {ticker.upper().removesuffix(".IS"): fallback or {}})[ticker.upper().removesuffix(".IS")]
