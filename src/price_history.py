import math
import yfinance as yf
import psycopg2
import psycopg2.extras
import threading
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import os

from src.config import get_config

load_dotenv()

_conn = None
_conn_lock = threading.Lock()
_yfinance_lock = threading.Lock()
_last_yfinance_request = 0.0
_db_initialized = False

# - period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
# - interval: 1m, 2m, 5m, 15m, 30m, 60m, 1h, 1d, 5d, 1wk, 1mo, 3mo
# - Dönen: [{ts, open, high, low, close, volume}, ...]

# ---------------------------------------------------------------------------
# PostgreSQL baglantisi (lazy)
# ---------------------------------------------------------------------------

def _get_conn():
    global _conn
    if _conn is None:
        cfg = get_config()["price_history"]
        _conn = psycopg2.connect(
            host=cfg["postgres_host"],
            port=cfg["postgres_port"],
            user=cfg["postgres_user"],
            password=os.getenv("POSTGRES_PASSWORD"),
            dbname=cfg["postgres_db"],
        )
        _conn.autocommit = True
    return _conn


def _init_db():
    global _db_initialized
    if _db_initialized:
        return
    with _conn_lock:
        if _db_initialized:
            return
        conn = _get_conn()
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


# ---------------------------------------------------------------------------
# Period → tarih araligi cevirisi
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Rate-limited yfinance cagrisi
# ---------------------------------------------------------------------------

def _rate_limited_fetch(ticker: str, interval: str, start: datetime, end: datetime):
    global _last_yfinance_request
    delay = get_config()["price_history"]["rate_limit_delay"]

    with _yfinance_lock:
        now = time.time()
        elapsed = now - _last_yfinance_request
        if elapsed < delay:
            time.sleep(delay - elapsed)

        data = yf.Ticker(ticker).history(start=start, end=end, interval=interval)
        _last_yfinance_request = time.time()
        return data


# ---------------------------------------------------------------------------
# Ana fonksiyon
# ---------------------------------------------------------------------------

def get_price_history(ticker: str, period: str, interval: str) -> list[dict]:
    _init_db()
    ticker = ticker.upper()
    if not ticker.endswith(".IS"):
        ticker = f"{ticker}.IS"

    start, end = _parse_period(period)
    conn = _get_conn()

    # 1. PostgreSQL'de var olani oku
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM price_candles "
            "WHERE ticker = %s AND interval = %s AND ts >= %s AND ts <= %s "
            "ORDER BY ts",
            (ticker, interval, start, end),
        )
        rows = cur.fetchall()

    db_rows = {row["ts"]: row for row in rows}

    # 2. Eksik timestamp'leri bul (yfinance verisiyle karsilastir)
    if rows:
        db_start = rows[0]["ts"]
        db_end = rows[-1]["ts"]
        # db_start ve db_end'in disinda bir sey kalmis mi kontrol et
        missing_start = db_start > start
        missing_end = db_end < end
    else:
        missing_start = True
        missing_end = False
        db_start = None
        db_end = None

    # 3. Eksik kisimlari yfinance'dan cek
    if not rows:
        _fetch_and_store(conn, ticker, interval, start, end)
    else:
        if missing_start:
            _fetch_and_store(conn, ticker, interval, start, db_start)
        if missing_end:
            _fetch_and_store(conn, ticker, interval, db_end, end)

    # 4. Tekrar oku (simdi tam)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM price_candles "
            "WHERE ticker = %s AND interval = %s AND ts >= %s AND ts <= %s "
            "ORDER BY ts",
            (ticker, interval, start, end),
        )
        rows = cur.fetchall()

    return [
        {
            "ts": row["ts"].isoformat(),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
        for row in rows
    ]


def _fetch_and_store(conn, ticker: str, interval: str, start: datetime, end: datetime):
    data = _rate_limited_fetch(ticker, interval, start, end)
    if data.empty:
        return

    with conn.cursor() as cur:
        for ts, row in data.iterrows():
            if row["Open"] is None or row["Close"] is None:
                continue
            volume = row["Volume"]
            if isinstance(volume, (float, int)) and not math.isnan(volume):
                volume = int(volume)
            else:
                volume = 0

            cur.execute(
                "INSERT INTO price_candles (ticker, interval, ts, open, high, low, close, volume) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (ticker, interval, ts) DO NOTHING",
                (
                    ticker,
                    interval,
                    ts.to_pydatetime(),
                    float(row["Open"]),
                    float(row["High"]),
                    float(row["Low"]),
                    float(row["Close"]),
                    volume,
                ),
            )
