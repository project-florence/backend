-- 013: LLM saglayici/model altyapisi (Adim 1: temel katman) — llm_providers,
-- llm_settings tablolari. init_db ile senkron (src/core/database.py);
-- idempotent (CREATE IF NOT EXISTS deseni).
--
-- Not: REFACTOR_PLAN.md bu dosyayi "012_llm_settings.sql" olarak
-- adlandirmisti ama 012 numarasi bu plan yazildiktan sonra ticker_health
-- (2026-08-2x) tarafindan alindi; sira burada 013 olarak devam ediyor.
--
-- Tasarim: florence/REFACTOR_PLAN.md Bolum 2.2 (sir yonetimi) ve 2.3 (sema).
-- llm_providers: saglayici basina kimlik bilgisi. api_key_encrypted AES-256-GCM
-- ile sifrelenir (bkz. src/llm/crypto.py); ana anahtar FLORENCE_MASTER_KEY
-- ortam degiskeninde, veritabaninin DISINDA tutulur. base_url yalniz
-- "openai-compatible" saglayicisi icin kullanilir (digerlerinde katalog
-- sabiti gecerlidir, bkz. src/llm/providers.py).
-- llm_settings: amac basina (digest | report | embedding) hangi saglayici +
-- model + ek parametrelerin secili oldugu.
--
-- Bu migration digest/rapor akislarina BAGLANMAZ (Adim 2'nin isi) -- yalniz
-- semayi kurar.

CREATE TABLE IF NOT EXISTS llm_providers (
    provider          TEXT PRIMARY KEY,
    api_key_encrypted BYTEA,
    base_url          TEXT,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS llm_settings (
    purpose    TEXT PRIMARY KEY,
    provider   TEXT NOT NULL REFERENCES llm_providers(provider),
    model      TEXT NOT NULL,
    params     JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT
);
