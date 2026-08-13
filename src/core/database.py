"""Async PostgreSQL erisimi: psycopg3 + baglanti havuzu.

Eski thread-local psycopg2 proxy'sinin yerini alir. Her is (API istegi, cron
gorevi, script) kendi baglantisini havuzdan alir; ContextVar icinde tutulur ve
``commit()`` / ``rollback()`` / ``release_current()`` cagrisiyla havuza iade
edilir. Boylece eski ``cursor()`` -> ``commit()`` cagri duzeni korunurken tam
async + havuzlu olur.

Dikkat: baglanti iadesi yapilmadan is biterse (ornegin hizli HTTPException
yolu), baglanti havuzdan disarida kalir. API tarafinda main.py middleware'i
``release_current()`` cagirarak bunu garantiler; cron gorevleri ve scriptler
kendi commit/rollback'lerini yapar.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar

from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

load_dotenv()

_pool: AsyncConnectionPool | None = None
_pool_lock = asyncio.Lock()

# Aktif is (task) basina bir baglanti. Task sonlaninca deger kaybolur; havuz
# iadesi commit/rollback/release_current ile yapilir.
_current_conn: ContextVar[AsyncConnection | None] = ContextVar(
    "db_current_conn", default=None
)

# cursor() icin sentinel: arguman verilmezse dict_row, acikca None verilirse
# tuple satirlar (row[0] erisimi).
_NO_FACTORY = object()


def _conninfo() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST')} port={os.getenv('POSTGRES_PORT')} "
        f"user={os.getenv('POSTGRES_USER')} password={os.getenv('POSTGRES_PASSWORD')} "
        f"dbname={os.getenv('POSTGRES_DB')}"
    )


async def _get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                pool = AsyncConnectionPool(
                    conninfo=_conninfo(),
                    min_size=1,
                    max_size=int(os.getenv("POSTGRES_POOL_MAX", "20")),
                    open=False,
                )
                await pool.open()
                _pool = pool
    return _pool


async def _get_conn() -> AsyncConnection:
    conn = _current_conn.get()
    if conn is None:
        pool = await _get_pool()
        conn = await pool.getconn()
        _current_conn.set(conn)
    return conn


async def _release_conn() -> None:
    conn = _current_conn.get()
    if conn is None:
        return
    _current_conn.set(None)
    try:
        # Bekleyen (commit edilmemis) islem varsa geri al; havuz temiz kalsin.
        await conn.rollback()
    except Exception:
        pass
    try:
        pool = await _get_pool()
        await pool.putconn(conn)
    except Exception:
        pass


class _AsyncDatabase:
    """Eski ``db`` nesnesinin async karsiligi.

    ``db.cursor()`` -> async context manager (varsayilan satirlar dict).
    ``db.commit()`` / ``db.rollback()`` -> aktif baglantiya uygular ve iade eder.
    ``db.release_current()`` -> aktif baglantiyi (varsa) iade eder.
    """

    @asynccontextmanager
    async def cursor(self, row_factory=_NO_FACTORY):
        conn = await _get_conn()
        factory = dict_row if row_factory is _NO_FACTORY else row_factory
        async with conn.cursor(row_factory=factory) as cur:
            yield cur

    async def commit(self) -> None:
        conn = _current_conn.get()
        if conn is not None:
            await conn.commit()
            await _release_conn()

    async def rollback(self) -> None:
        conn = _current_conn.get()
        if conn is not None:
            await conn.rollback()
            await _release_conn()

    async def release_current(self) -> None:
        await _release_conn()

    async def close(self) -> None:
        """Havuzu kapatir (uygulama kapanisinda)."""
        global _pool
        await _release_conn()
        pool = _pool
        _pool = None
        if pool is not None:
            try:
                await pool.close()
            except Exception:
                pass


db = _AsyncDatabase()


async def init_db() -> None:
    """Runtime schema kaynagi. API startup'inda ve scriptlerde cagrilir."""
    conn = await _get_conn()
    try:
        async with conn.cursor() as cur:  # tuple satirlar (row[0] erisimi icin)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(255) UNIQUE NOT NULL,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    hashed_pw TEXT NOT NULL,
                    credits DOUBLE PRECISION NOT NULL DEFAULT 5
                );
                ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS user_type VARCHAR(50) NOT NULL DEFAULT 'user';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_announcement_viewed_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
                ALTER TABLE users ADD COLUMN IF NOT EXISTS is_frozen BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_token TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_expires_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_id VARCHAR(30) NOT NULL DEFAULT 'avatar-1';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES users(id);
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ;
            """)
            # Backfill: email_verify_token'i olmayan dogrulanmamis kullanicilar
            # (dogrulama sistemi oncesi kayitlar) onayli sayilir. Yeni kayitlar
            # register'da token urettigi icin etkilenmez. Idempotent.
            await cur.execute(
                "UPDATE users SET email_verified = TRUE "
                "WHERE email_verified = FALSE AND email_verify_token IS NULL"
            )
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS tickers (
                    code TEXT PRIMARY KEY,
                    name TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS companies (
                    ticker TEXT PRIMARY KEY REFERENCES tickers(code),
                    name TEXT,
                    summary_page TEXT,
                    city TEXT,
                    auditor TEXT,
                    company_id TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_stats (
                    ticker TEXT PRIMARY KEY,
                    info_count INTEGER NOT NULL DEFAULT 0,
                    report_count INTEGER NOT NULL DEFAULT 0,
                    news_count INTEGER NOT NULL DEFAULT 0,
                    history_count INTEGER NOT NULL DEFAULT 0,
                    simulation_count INTEGER NOT NULL DEFAULT 0,
                    favorite_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id INT REFERENCES users(id) ON DELETE CASCADE,
                    ticker_code TEXT REFERENCES tickers(code) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, ticker_code)
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES users(id) ON DELETE CASCADE,
                    ticker TEXT NOT NULL,
                    type TEXT NOT NULL,
                    title TEXT,
                    token_usage JSONB,
                    content TEXT NOT NULL,
                    sentiments JSONB DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_reports_user_id ON reports(user_id);
                ALTER TABLE reports ADD COLUMN IF NOT EXISTS title TEXT;
                ALTER TABLE reports ADD COLUMN IF NOT EXISTS token_usage JSONB;
                ALTER TABLE reports ADD COLUMN IF NOT EXISTS sentiments JSONB DEFAULT '[]'::jsonb;
                ALTER TABLE reports ADD COLUMN IF NOT EXISTS purpose TEXT;
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS simulations (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES users(id) ON DELETE CASCADE,
                    ticker TEXT NOT NULL,
                    days INT NOT NULL,
                    bounds TEXT,
                    target TEXT,
                    result JSONB NOT NULL,
                    cost NUMERIC,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_simulations_user_id ON simulations(user_id);
                ALTER TABLE simulations ADD COLUMN IF NOT EXISTS bounds TEXT;
                ALTER TABLE simulations ADD COLUMN IF NOT EXISTS target TEXT;
                ALTER TABLE simulations ADD COLUMN IF NOT EXISTS cost NUMERIC;
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS user_credits (
                    user_id INT REFERENCES users(id) ON DELETE CASCADE,
                    credit_type VARCHAR(50) NOT NULL DEFAULT 'free_credits',
                    amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, credit_type)
                );
                CREATE INDEX IF NOT EXISTS idx_user_credits_user_id ON user_credits(user_id);
            """)
            await cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'credits'")
            if await cur.fetchone():
                await cur.execute("""
                    INSERT INTO user_credits (user_id, credit_type, amount)
                    SELECT id, 'free_credits', credits FROM users
                    ON CONFLICT (user_id, credit_type) DO NOTHING
                """)
                await cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS credits")
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(50) NOT NULL,
                    user_id INT REFERENCES users(id) ON DELETE SET NULL,
                    session_id VARCHAR(100),
                    ticker VARCHAR(20),
                    details JSONB DEFAULT '{}',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_analytics_event_type ON analytics_events(event_type);
                CREATE INDEX IF NOT EXISTS idx_analytics_user_id ON analytics_events(user_id);
                CREATE INDEX IF NOT EXISTS idx_analytics_ticker ON analytics_events(ticker);
                CREATE INDEX IF NOT EXISTS idx_analytics_created_at ON analytics_events(created_at);
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_vectors (
                    ticker TEXT PRIMARY KEY,
                    risk DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                    horizon DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                    profitability DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id SERIAL PRIMARY KEY,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    endpoint TEXT DEFAULT 'unknown',
                    user_id INT REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS macroeconomy (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    usa_gdp NUMERIC NOT NULL DEFAULT 0,
                    usa_real_gdp NUMERIC NOT NULL DEFAULT 0,
                    fed_funds NUMERIC NOT NULL DEFAULT 0,
                    fed_funds_rate NUMERIC NOT NULL DEFAULT 0,
                    usa_unrate NUMERIC NOT NULL DEFAULT 0,
                    brent_crude_oil_price NUMERIC NOT NULL DEFAULT 0,
                    wti_crude_oil_price NUMERIC NOT NULL DEFAULT 0,
                    usa_consumer_cpi NUMERIC NOT NULL DEFAULT 0,
                    usa_10y_treasury NUMERIC NOT NULL DEFAULT 0,
                    dxy NUMERIC NOT NULL DEFAULT 0,
                    vix NUMERIC NOT NULL DEFAULT 0,
                    sp500 NUMERIC NOT NULL DEFAULT 0,
                    nasdaq NUMERIC NOT NULL DEFAULT 0,
                    bitcoin NUMERIC NOT NULL DEFAULT 0
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS market_rates (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    data_type TEXT NOT NULL,
                    data JSONB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_market_rates_type ON market_rates(data_type);
                CREATE INDEX IF NOT EXISTS idx_market_rates_ts ON market_rates(timestamp);
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    prefs JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS portfolios (
                    portfolio_id TEXT PRIMARY KEY,
                    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    portfolio JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_portfolios_user_id ON portfolios(user_id);
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS economy_rates (
                    ticker TEXT NOT NULL,
                    ts TIMESTAMPTZ NOT NULL,
                    price JSONB NOT NULL,
                    PRIMARY KEY (ticker, ts)
                );
            """)
            # Self-heal: eski ortamlar economy_rates.price'i DOUBLE PRECISION
            # olarak yaratmis olabilir (migration 002). Kod JSONB bekliyor.
            await cur.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'economy_rates' AND column_name = 'price'"
            )
            economy_price_row = await cur.fetchone()
            if economy_price_row and economy_price_row[0] != "jsonb":
                await cur.execute(
                    "ALTER TABLE economy_rates ALTER COLUMN price TYPE JSONB USING to_jsonb(price::text)"
                )
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS announcements (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sent_by INT REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS cron_jobs (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    interval_ms BIGINT NOT NULL,
                    last_run TIMESTAMPTZ
                );
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    id SERIAL PRIMARY KEY,
                    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    device TEXT,
                    expires_at TIMESTAMPTZ NOT NULL,
                    revoked_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id ON refresh_tokens(user_id);
            """)
            await cur.execute("""
                CREATE OR REPLACE FUNCTION create_default_prefs()
                RETURNS TRIGGER AS $$
                BEGIN
                    INSERT INTO user_preferences (user_id, prefs)
                    VALUES (NEW.id, '{"layout": "default", "theme": "default", "language": "default"}'::jsonb);
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;

                DROP TRIGGER IF EXISTS trg_user_prefs ON users;
                CREATE TRIGGER trg_user_prefs
                AFTER INSERT ON users
                FOR EACH ROW
                EXECUTE FUNCTION create_default_prefs();
            """)
            await cur.execute("""
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
                CREATE INDEX IF NOT EXISTS idx_price_candles_lookup
                ON price_candles (ticker, interval, ts DESC)
            """)
        await conn.commit()
    finally:
        await _release_conn()


# Mum yazimlarini (cron + API) deadlock'a dusuren eszamanli INSERT'leri
# onlemek icin surec ici async yazma kilidi.
price_write_lock = asyncio.Lock()
