-- 009: Doviz & degerli metaller veri hatti (Faz 1) — rate_candles, rate_metrics,
-- rate_provider_status + kaynak izleme ALTER'leri.
-- init_db ile senkron (src/core/database.py); idempotent (CREATE IF NOT EXISTS /
-- ADD COLUMN IF NOT EXISTS deseni). Tasarim: ANALYSIS/ekonomi-refactor-plani.md Bolum 5.1.

-- 1) FX/metal mumlari (yfinance gecmis + gunluk kapanis; price_candles deseni)
CREATE TABLE IF NOT EXISTS rate_candles (
    symbol   TEXT NOT NULL,          -- KANONIK sembol: 'USD', 'XAU-ONS', 'XAU-GRAM'
    interval TEXT NOT NULL,          -- '1d' (v1); ileride '1h'
    ts       TIMESTAMPTZ NOT NULL,
    open     DOUBLE PRECISION,
    high     DOUBLE PRECISION,
    low      DOUBLE PRECISION,
    close    DOUBLE PRECISION,
    volume   DOUBLE PRECISION,
    source   TEXT,                   -- 'genelpara' | 'yfinance_metals' | ...
    PRIMARY KEY (symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_rate_candles_lookup
    ON rate_candles (symbol, interval, ts DESC);

-- 2) Gunluk analiz ozetleri (cron on-hesaplar; API istek aninda hesaplamaz)
CREATE TABLE IF NOT EXISTS rate_metrics (
    symbol      TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    analysis    JSONB NOT NULL,      -- AnalysisResult.model_dump_json()
    PRIMARY KEY (symbol, computed_at)
);
CREATE INDEX IF NOT EXISTS idx_rate_metrics_symbol ON rate_metrics (symbol, computed_at DESC);

-- 3) Kaynak saglik/duragan durum tablosu (devre kesici kaliciligi)
CREATE TABLE IF NOT EXISTS rate_provider_status (
    provider   TEXT PRIMARY KEY,     -- 'genelpara' | 'tcmb' | ...
    last_success TIMESTAMPTZ,
    last_error   TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    circuit_open  BOOLEAN NOT NULL DEFAULT FALSE,
    last_error_msg TEXT
);

-- 4) Mevcut economy_rates'e kaynak sutunu (PK cakismasi cozumu + audit)
ALTER TABLE economy_rates ADD COLUMN IF NOT EXISTS source TEXT;

-- 5) market_rates'e kaynak + kota (opsiyonel, izleme icin)
ALTER TABLE market_rates ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE market_rates ADD COLUMN IF NOT EXISTS meta JSONB;  -- {remaining, provider, tz}