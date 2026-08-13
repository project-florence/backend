-- 006: Admin/freeze, e-posta dogrulama, avatar ve rapor amaci kolonlari.
-- Tüm DDL idempotent: mevcut veritabanlarinda guvenle tekrar calisir.
-- (Runtime schema kaynagi src/core/database.py:init_db() ile senkron tutulur.)

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_frozen BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_token TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_expires_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_id VARCHAR(30) NOT NULL DEFAULT 'avatar-1';

ALTER TABLE reports ADD COLUMN IF NOT EXISTS purpose TEXT;
