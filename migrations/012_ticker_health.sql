-- 012: Olu ticker bastirma (dead-ticker suppression) — ticker_health tablosu.
-- init_db ile senkron (src/core/database.py); idempotent (CREATE IF NOT EXISTS
-- deseni). BIST evreninde artik upstream'de (yfinance) bulunmayan ticker'lar
-- icin ardisik basarisizlik sayaci + gecici bastirma penceresi tutar; bkz.
-- src/services/ticker_health.py. rate_provider_status (009) ile ayni desen,
-- provider yerine ticker bazinda.

CREATE TABLE IF NOT EXISTS ticker_health (
    ticker TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_failure_kind TEXT,
    last_error TEXT,
    suppressed_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ticker_health_suppressed
    ON ticker_health (suppressed_until) WHERE suppressed_until IS NOT NULL;
