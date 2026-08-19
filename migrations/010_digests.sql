-- 010: Gunluk piyasa bultenleri (market digest) — digests tablosu.
-- init_db ile senkron (src/core/database.py); idempotent (CREATE IF NOT EXISTS
-- deseni). Her slot (morning 09:45 / noon 13:15 / evening 18:45 TRT) icin
-- gunluk bir satir; (date, slot) cifti uzerinden duplicate onleme yapilir.

CREATE TABLE IF NOT EXISTS digests (
    id TEXT PRIMARY KEY,
    date DATE NOT NULL,
    slot TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT,
    sections JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    language TEXT NOT NULL DEFAULT 'tr',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_digests_date_slot ON digests (date, slot);