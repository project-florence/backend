-- 008: Veri disa aktarim (Google Takeout tarzi) tablosu.
-- init_db ile senkron; idempotent.
CREATE TABLE IF NOT EXISTS exports (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    year INT NOT NULL,
    format TEXT NOT NULL CHECK (format IN ('csv','json')),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','ready','sent','failed')),
    file_path TEXT,
    token TEXT UNIQUE,
    expires_at TIMESTAMPTZ,
    row_count INT,
    size_bytes BIGINT,
    downloaded_count INT NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_exports_user_id ON exports(user_id);
CREATE INDEX IF NOT EXISTS idx_exports_status ON exports(status);
CREATE INDEX IF NOT EXISTS idx_exports_token ON exports(token);
