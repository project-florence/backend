-- 011: Sifre sifirlama token'lari — password_resets tablosu.
-- init_db ile senkron (src/core/database.py); idempotent (CREATE IF NOT EXISTS
-- deseni). Kullanici sifresini unutursa /auth/forgot-password ile hashed bir
-- token uretilir; /auth/reset-password bu token ile sifreyi degistirir.
-- Token tek kullanimliktir (used_at doldugunda) ve sureli gecerlidir (expires_at).

CREATE TABLE IF NOT EXISTS password_resets (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_password_resets_user_id ON password_resets(user_id);
CREATE INDEX IF NOT EXISTS idx_password_resets_token_hash ON password_resets(token_hash);
