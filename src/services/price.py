import json
import math
import time
import psycopg2.extras
from datetime import datetime, timedelta, timezone

from src.clients.yfinance import fetch_price_history
from src.core.database import db, price_write_lock
from src.core.redis import r

INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "1h"}


def _clean(val):
    if val is None:
        return None
    if hasattr(val, "item"):
        val = val.item()
    if isinstance(val, float):
        return None if math.isnan(val) else val
    return val


def _acquire_refresh_lock(ticker: str, interval: str, ttl: int = 15) -> bool:
    lock_key = f"refresh_lock:{ticker}:{interval}"
    return r.set(lock_key, "1", ex=ttl, nx=True) is not None


def invalidate_price_cache(ticker: str, interval: str):
    r.delete(_current_price_cache_key(ticker, interval))


_db_initialized = False


def _init_db():
    global _db_initialized
    if _db_initialized:
        return
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_candles (
                ticker    TEXT NOT NULL,
                interval  TEXT NOT NULL,
                ts        TIMESTAMPTZ NOT NULL,
                open      DOUBLE PRECISION,
                high      DOUBLE PRECISION,
                low       DOUBLE PRECISION,
                close     DOUBLE PRECISION,
                volume    BIGINT,
                PRIMARY KEY (ticker, interval, ts)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_price_candles_lookup
            ON price_candles (ticker, interval, ts DESC)
        """)
    _db_initialized = True


def _parse_period(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = now
    period = period.lower()

    if period == "ytd":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "max":
        start = now - timedelta(days=3650)
    elif period.endswith("d"):
        start = now - timedelta(days=int(period[:-1]))
    elif period.endswith("mo"):
        start = now - timedelta(days=int(period[:-2]) * 30)
    elif period.endswith("y"):
        start = now - timedelta(days=int(period[:-1]) * 365)
    else:
        raise ValueError(f"Invalid period: {period}")

    return start, end


def _fetch_and_store(conn, ticker: str, interval: str, start: datetime, end: datetime):
    data = fetch_price_history(ticker, interval, start, end)
    if data.empty:
        return

    values = []
    for ts, row in data.iterrows():
        if _clean(row.get("Open")) is None or _clean(row.get("Close")) is None:
            continue
        volume = row.get("Volume")
        if isinstance(volume, (float, int)) and not math.isnan(volume):
            volume = int(volume)
        else:
            volume = 0
        values.append((
            ticker, interval, ts.to_pydatetime(),
            _clean(row.get("Open")), _clean(row.get("High")),
            _clean(row.get("Low")), _clean(row.get("Close")), volume,
        ))

    if not values:
        return

    with price_write_lock:
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur,
                    "INSERT INTO price_candles (ticker, interval, ts, open, high, low, close, volume) VALUES %s "
                    "ON CONFLICT (ticker, interval, ts) DO UPDATE SET "
                    "open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, "
                    "close = EXCLUDED.close, volume = EXCLUDED.volume",
                    values,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


_PERIOD_DAYS: dict[str, int] = {
    "1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825, "10y": 3650,
}

_MAX_PERIOD_FOR_INTERVAL: dict[str, int] = {
    "1m": 7, "5m": 60, "15m": 60, "30m": 60, "1h": 730, "1d": 3650, "5d": 3650, "1wk": 3650, "1mo": 3650, "3mo": 3650,
}


def _validate_interval_period(interval: str, period: str):
    max_days = _MAX_PERIOD_FOR_INTERVAL.get(interval)
    if max_days is None:
        raise ValueError(f"Invalid interval: {interval}")
    period_days = _PERIOD_DAYS.get(period)
    if period_days is None:
        days = 0
        if period.endswith("d"):
            days = int(period[:-1])
        elif period.endswith("mo"):
            days = int(period[:-2]) * 30
        elif period.endswith("y"):
            days = int(period[:-1]) * 365
        elif period == "ytd":
            from datetime import datetime, timezone
            days = (datetime.now(timezone.utc) - datetime.now(timezone.utc).replace(month=1, day=1)).days
        elif period == "max":
            days = 3650
        else:
            raise ValueError(f"Invalid period: {period}")
        period_days = days
    if period_days > max_days:
        raise ValueError(
            f"Interval '{interval}' only supports up to {max_days} days of data, "
            f"but period '{period}' requires {period_days} days"
        )


def _cache_key(ticker: str, period: str, interval: str) -> str:
    return f"price_history:{ticker}:{period}:{interval}"


def get_price_history(ticker: str, period: str, interval: str, hot: bool = False) -> list[dict]:
    from src.core.config import get_config

    _validate_interval_period(interval, period)

    cfg = get_config().get("price_history", {})
    cache_ttl = cfg.get("cache_ttl_hot" if hot else "cache_ttl", 0)
    stale_ttl = cfg.get("stale_ttl", 0)

    ticker = ticker.upper()
    if not ticker.endswith(".IS"):
        ticker = f"{ticker}.IS"

    if cache_ttl > 0:
        cached = r.get(_cache_key(ticker, period, interval))
        if cached:
            try:
                entry = json.loads(cached)
                if isinstance(entry, dict) and "d" in entry and "t" in entry:
                    age = time.time() - entry["t"]
                    if age <= cache_ttl + stale_ttl:
                        return entry["d"]
                elif isinstance(entry, list):
                    return entry
            except (json.JSONDecodeError, TypeError):
                pass

    _init_db()

    start, end = _parse_period(period)
    conn = db.get_connection()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM price_candles "
            "WHERE ticker = %s AND interval = %s AND ts >= %s AND ts <= %s ORDER BY ts",
            (ticker, interval, start, end),
        )
        rows = cur.fetchall()

    fetched = False
    if rows:
        db_start = rows[0]["ts"]
        db_end = rows[-1]["ts"]
        if db_start > start:
            _fetch_and_store(conn, ticker, interval, start, db_start)
            fetched = True
        if db_end < end:
            _fetch_and_store(conn, ticker, interval, db_end, end)
            fetched = True
    else:
        _fetch_and_store(conn, ticker, interval, start, end)
        fetched = True

    if fetched:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT ts, open, high, low, close, volume FROM price_candles "
                "WHERE ticker = %s AND interval = %s AND ts >= %s AND ts <= %s ORDER BY ts",
                (ticker, interval, start, end),
            )
            rows = cur.fetchall()

    result = [
        {"ts": row["ts"].isoformat(), "open": _clean(row["open"]), "high": _clean(row["high"]),
         "low": _clean(row["low"]), "close": _clean(row["close"]), "volume": row["volume"]}
        for row in rows
        if _clean(row["close"]) is not None
    ]

    if cache_ttl > 0:
        cache_entry = json.dumps({"d": result, "t": time.time()}, ensure_ascii=False)
        r.set(_cache_key(ticker, period, interval), cache_entry, ex=cache_ttl + stale_ttl)

    return result


def _current_price_cache_key(ticker: str, interval: str) -> str:
    return f"latest_price:{ticker}:{interval}"


def get_current_price(ticker: str, interval: str = "5m") -> float | None:
    ticker = ticker.upper()
    if not ticker.endswith(".IS"):
        ticker = f"{ticker}.IS"

    intraday = interval in INTRADAY_INTERVALS

    if intraday:
        cached = r.get(_current_price_cache_key(ticker, interval))
        if cached is not None:
            try:
                return float(cached)
            except (TypeError, ValueError):
                pass

    _init_db()
    conn = db.get_connection()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT close, ts FROM price_candles "
            "WHERE ticker = %s AND interval = %s ORDER BY ts DESC LIMIT 1",
            (ticker, interval),
        )
        row = cur.fetchone()

    if row and _clean(row["close"]) is not None:
        price = float(row["close"])
        if intraday:
            r.set(_current_price_cache_key(ticker, interval), str(price), ex=30)
        return price

    if intraday:
        if _acquire_refresh_lock(ticker, interval):
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=1)
            _fetch_and_store(conn, ticker, interval, start, now)

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT close FROM price_candles "
                    "WHERE ticker = %s AND interval = %s ORDER BY ts DESC LIMIT 1",
                    (ticker, interval),
                )
                row = cur.fetchone()
            if row and _clean(row["close"]) is not None:
                price = float(row["close"])
                r.set(_current_price_cache_key(ticker, interval), str(price), ex=30)
                return price

        return get_current_price(ticker, "1d")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT close FROM price_candles "
            "WHERE ticker = %s AND interval = '1d' ORDER BY ts DESC LIMIT 1",
            (ticker,),
        )
        row = cur.fetchone()
    if row and _clean(row["close"]) is not None:
        return float(row["close"])

    prices = get_price_history(ticker, "5d", "1d")
    if prices:
        return float(prices[-1]["close"])
    return None
