-- 014: LLM gozlemlenebilirlik (Adim 3) -- token_usage'a basari/hata izleme
-- kolonlari. init_db ile senkron (src/core/database.py); idempotent
-- (ADD COLUMN IF NOT EXISTS deseni).
--
-- Neden: 2026-08-26'da market digest'in ucu slotu da sessizce basarisiz oldu
-- (REFACTOR_PLAN.md Bolum 0) -- hata yalniz ucucu container logundaydi,
-- token_usage'a digest hic yazmiyordu. Bu migration her LLM cagrisinin
-- (basarili VEYA basarisiz) kalici, sorgulanabilir bir satir birakmasini
-- saglayan semayi kurar.
--
-- endpoint kolonu SILINMEDI: eski satirlar var, geriye donuk uyum icin
-- korunuyor. Yeni yazimlarda purpose ile ayni degerle doldurulur (bkz.
-- src/services/token.py::log_token_usage docstring'i) -- boylece endpoint
-- filtresine dayanan eski sorgular calismaya devam eder.

-- Basarisiz bir cagrida token sayilari bilinmiyor (istek hic tamamlanmadi),
-- bu yuzden var olan NOT NULL kisitini gevsetiyoruz -- yoksa hata satirinin
-- INSERT'i kendisi basarisiz olurdu.
ALTER TABLE token_usage ALTER COLUMN prompt_tokens DROP NOT NULL;
ALTER TABLE token_usage ALTER COLUMN completion_tokens DROP NOT NULL;
ALTER TABLE token_usage ALTER COLUMN total_tokens DROP NOT NULL;
ALTER TABLE token_usage ALTER COLUMN prompt_tokens DROP DEFAULT;
ALTER TABLE token_usage ALTER COLUMN completion_tokens DROP DEFAULT;
ALTER TABLE token_usage ALTER COLUMN total_tokens DROP DEFAULT;

ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS purpose TEXT;
ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS duration_ms INTEGER;

CREATE INDEX IF NOT EXISTS idx_token_usage_purpose ON token_usage(purpose);
CREATE INDEX IF NOT EXISTS idx_token_usage_status ON token_usage(status);
CREATE INDEX IF NOT EXISTS idx_token_usage_created_at ON token_usage(created_at);
