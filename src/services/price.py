import math
import psycopg2.extras
from datetime import datetime, timedelta, timezone

from src.clients.yfinance import fetch_price_history
from src.core.database import db


def _clean(val):
    if val is None:
        return None
    if hasattr(val, "item"):
        val = val.item()
    if isinstance(val, float):
        return None if math.isnan(val) else val
    return val

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

    with conn.cursor() as cur:
        for ts, row in data.iterrows():
            if _clean(row.get("Open")) is None or _clean(row.get("Close")) is None:
                continue
            volume = row.get("Volume")
            if isinstance(volume, (float, int)) and not math.isnan(volume):
                volume = int(volume)
            else:
                volume = 0
            cur.execute(
                "INSERT INTO price_candles (ticker, interval, ts, open, high, low, close, volume) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (ticker, interval, ts) DO NOTHING",
                (ticker, interval, ts.to_pydatetime(),
                 _clean(row.get("Open")), _clean(row.get("High")),
                 _clean(row.get("Low")), _clean(row.get("Close")), volume),
            )


def get_price_history(ticker: str, period: str, interval: str) -> list[dict]:
    _init_db()
    ticker = ticker.upper()
    if not ticker.endswith(".IS"):
        ticker = f"{ticker}.IS"

    start, end = _parse_period(period)
    conn = db.get_connection()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM price_candles "
            "WHERE ticker = %s AND interval = %s AND ts >= %s AND ts <= %s ORDER BY ts",
            (ticker, interval, start, end),
        )
        rows = cur.fetchall()

    if rows:
        db_start = rows[0]["ts"]
        db_end = rows[-1]["ts"]
        has_missing_start = db_start > start
        has_missing_end = db_end < end
    else:
        has_missing_start = True
        has_missing_end = False
        db_start = None

    if not rows:
        _fetch_and_store(conn, ticker, interval, start, end)
    else:
        if has_missing_start:
            _fetch_and_store(conn, ticker, interval, start, db_start)
        if has_missing_end:
            _fetch_and_store(conn, ticker, interval, db_end, end)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM price_candles "
            "WHERE ticker = %s AND interval = %s AND ts >= %s AND ts <= %s ORDER BY ts",
            (ticker, interval, start, end),
        )
        rows = cur.fetchall()

    return [
        {"ts": row["ts"].isoformat(), "open": _clean(row["open"]), "high": _clean(row["high"]),
         "low": _clean(row["low"]), "close": _clean(row["close"]), "volume": row["volume"]}
        for row in rows
        if _clean(row["close"]) is not None
    ]
