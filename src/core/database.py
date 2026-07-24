import psycopg2
import threading
import os
from dotenv import load_dotenv
from src.core.config import get_config

load_dotenv()


def _pg_host():
    return os.getenv("POSTGRES_HOST") or get_config()["postgres"]["host"]

def _pg_port():
    return os.getenv("POSTGRES_PORT") or get_config()["postgres"]["port"]

def _pg_user():
    return os.getenv("POSTGRES_USER") or get_config()["postgres"]["user"]

def _pg_default_db():
    return os.getenv("POSTGRES_DB") or get_config()["postgres"]["default_db"]


class _DatabaseProxy:
    _conns = {}
    _lock = threading.Lock()

    def _ensure_db_exists(self, db_name: str):
        if db_name == _pg_default_db():
            return
        conn = psycopg2.connect(
            host=_pg_host(),
            port=_pg_port(),
            user=_pg_user(),
            password=os.getenv("POSTGRES_PASSWORD"),
            dbname=_pg_default_db(),
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{db_name}"')
        conn.close()

    def get_connection(self, db_name=None):
        key = db_name or "default"
        if key not in self._conns:
            with self._lock:
                if key not in self._conns:
                    actual_db = db_name or _pg_default_db()
                    if db_name:
                        self._ensure_db_exists(db_name)
                    conn = psycopg2.connect(
                        host=_pg_host(),
                        port=_pg_port(),
                        user=_pg_user(),
                        password=os.getenv("POSTGRES_PASSWORD"),
                        dbname=actual_db,
                    )
                    conn.autocommit = False
                    self._conns[key] = conn
        return self._conns.get(key)

    def cursor(self, db_name=None, **kwargs):
        conn = self.get_connection(db_name)
        conn.rollback()
        return conn.cursor(**kwargs)

    def commit(self, db_name=None):
        self.get_connection(db_name).commit()

    def rollback(self, db_name=None):
        self.get_connection(db_name).rollback()


def init_db():
    conn = psycopg2.connect(
        host=_pg_host(),
        port=_pg_port(),
        user=_pg_user(),
        password=os.getenv("POSTGRES_PASSWORD"),
        dbname=_pg_default_db(),
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                hashed_pw TEXT NOT NULL,
                credits DOUBLE PRECISION NOT NULL DEFAULT 5
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tickers (
                code TEXT PRIMARY KEY,
                name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
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

        cur.execute("""
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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INT REFERENCES users(id) ON DELETE CASCADE,
                ticker_code TEXT REFERENCES tickers(code) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                
                PRIMARY KEY (user_id, ticker_code)
            );
        """)

        cur.execute("""
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
        """)

        cur.execute("""
        ALTER TABLE reports ADD COLUMN IF NOT EXISTS title TEXT;
        ALTER TABLE reports ADD COLUMN IF NOT EXISTS token_usage JSONB;
        ALTER TABLE reports ADD COLUMN IF NOT EXISTS sentiments JSONB DEFAULT '[]'::jsonb;
        """)

        cur.execute("""
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
        """)

        cur.execute("""
        ALTER TABLE simulations ADD COLUMN IF NOT EXISTS bounds TEXT;
        ALTER TABLE simulations ADD COLUMN IF NOT EXISTS target TEXT;
        ALTER TABLE simulations ADD COLUMN IF NOT EXISTS cost NUMERIC;
        """)

        cur.execute("""
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

        cur.execute("""
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


        cur.execute("""
        CREATE TABLE IF NOT EXISTS market_rates (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            data_type TEXT NOT NULL,
            data JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_market_rates_type ON market_rates(data_type);
        CREATE INDEX IF NOT EXISTS idx_market_rates_ts ON market_rates(timestamp);
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                prefs JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        cur.execute("""
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

    conn.close()


db = _DatabaseProxy()
