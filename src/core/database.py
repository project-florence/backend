from contextlib import contextmanager
import psycopg2
import psycopg2.sql
import threading
import os
from dotenv import load_dotenv
load_dotenv()


def _pg_host():
    return os.getenv("POSTGRES_HOST")

def _pg_port():
    return os.getenv("POSTGRES_PORT")

def _pg_user():
    return os.getenv("POSTGRES_USER")

def _pg_default_db():
    return os.getenv("POSTGRES_DB")


class _DatabaseProxy:
    _local = threading.local()
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
                cur.execute(psycopg2.sql.SQL("CREATE DATABASE {}").format(psycopg2.sql.Identifier(db_name)))
        conn.close()

    def get_connection(self, db_name=None):
        key = db_name or "default"
        thread_key = (key, threading.get_ident())
        if not hasattr(self._local, "conns"):
            self._local.conns = {}
        if thread_key not in self._local.conns:
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
            self._local.conns[thread_key] = conn
        return self._local.conns.get(thread_key)

    @contextmanager
    def cursor(self, db_name=None, **kwargs):
        conn = self.get_connection(db_name)
        cur = conn.cursor(**kwargs)
        try:
            yield cur
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

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
            ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS user_type VARCHAR(50) NOT NULL DEFAULT 'user';
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_announcement_viewed_at TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
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
            CREATE TABLE IF NOT EXISTS user_credits (
                user_id INT REFERENCES users(id) ON DELETE CASCADE,
                credit_type VARCHAR(50) NOT NULL DEFAULT 'free_credits',
                amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, credit_type)
            );
            CREATE INDEX IF NOT EXISTS idx_user_credits_user_id ON user_credits(user_id);
        """)

        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'credits'")
        if cur.fetchone():
            cur.execute("""
                INSERT INTO user_credits (user_id, credit_type, amount)
                SELECT id, 'free_credits', credits FROM users
                ON CONFLICT (user_id, credit_type) DO NOTHING
            """)
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS credits")

        cur.execute("""
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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_vectors (
                ticker TEXT PRIMARY KEY,
                risk DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                horizon DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                profitability DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
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
            CREATE TABLE IF NOT EXISTS portfolios (
                portfolio_id TEXT PRIMARY KEY,
                user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                portfolio JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_portfolios_user_id ON portfolios(user_id);
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS economy_rates (
                ticker TEXT NOT NULL,
                ts TIMESTAMPTZ NOT NULL,
                price JSONB NOT NULL,
                PRIMARY KEY (ticker, ts)
            );
        """)

        # Self-heal: eski ortamlar economy_rates.price'i DOUBLE PRECISION olarak
        # yaratmis olabilir (migration 002). Kod JSONB bekliyor; gerekirse cevir.
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'economy_rates' AND column_name = 'price'"
        )
        economy_price_row = cur.fetchone()
        if economy_price_row and economy_price_row[0] != "jsonb":
            cur.execute(
                "ALTER TABLE economy_rates ALTER COLUMN price TYPE JSONB USING to_jsonb(price::text)"
            )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS announcements (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                sent_by INT REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS cron_jobs (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                interval_ms BIGINT NOT NULL,
                last_run TIMESTAMPTZ
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


# Fiyat mumlarina yazan cron thread'leri ve API istekleri arasindaki
# eşzamanli INSERT deadlock'larini onlemek icin kullanilan surec ici yazma kilidi.
# (Tek API worker calistigimiz icin surec ici kilit yeterli.)
price_write_lock = threading.Lock()


db = _DatabaseProxy()
