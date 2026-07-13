import psycopg2
import threading
import os
from dotenv import load_dotenv
from src.core.config import get_config

load_dotenv()


class _DatabaseProxy:
    _conns = {}
    _lock = threading.Lock()

    def _ensure_db_exists(self, db_name: str):
        if db_name == get_config()["postgres"]["default_db"]:
            return
        conn = psycopg2.connect(
            host=get_config()["postgres"]["host"],
            port=get_config()["postgres"]["port"],
            user=get_config()["postgres"]["user"],
            password=os.getenv("POSTGRES_PASSWORD"),
            dbname=get_config()["postgres"]["default_db"],
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
                    cfg = get_config()["postgres"]
                    actual_db = db_name or cfg["default_db"]
                    if db_name:
                        self._ensure_db_exists(db_name)
                    conn = psycopg2.connect(
                        host=cfg["host"],
                        port=cfg["port"],
                        user=cfg["user"],
                        password=os.getenv("POSTGRES_PASSWORD"),
                        dbname=actual_db,
                    )
                    conn.autocommit = False
                    self._conns[key] = conn
        return self._conns.get(key)

    def cursor(self, db_name=None, **kwargs):
        return self.get_connection(db_name).cursor(**kwargs)

    def commit(self, db_name=None):
        self.get_connection(db_name).commit()

    def rollback(self, db_name=None):
        self.get_connection(db_name).rollback()


def init_db():
    conn = psycopg2.connect(
        host=get_config()["postgres"]["host"],
        port=get_config()["postgres"]["port"],
        user=get_config()["postgres"]["user"],
        password=os.getenv("POSTGRES_PASSWORD"),
        dbname=get_config()["postgres"]["default_db"],
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
                
                -- Bir kullanıcı aynı ticker'ı iki kez favorilemesin! 
                -- Bu ikisinin birleşimi Primary Key olur:
                PRIMARY KEY (user_id, ticker_code)
            );
        """)
    conn.close()


db = _DatabaseProxy()
