-- 007: Eski kullanicilar e-posta dogrulamasini tamamlamis sayilir.
-- Backfill oncesi kayitlarda email_verify_token NULL'dir; token'i olmayan
-- dogrulanmamis kullanicilar onaylanir. Yeni kayitlar register'da token
-- urettigi icin bu UPDATE'ten etkilenmez.
-- Ayrica bot sistemi icin owner_id ve last_login kolonlari (init_db ile
-- senkron; idempotent).
UPDATE users SET email_verified = TRUE
WHERE email_verified = FALSE AND email_verify_token IS NULL;

ALTER TABLE users ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES users(id);
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ;
