-- 015: llm_settings amac-basina secimden TEK SATIRLIK (singleton) ayara
-- gecer (REFACTOR_PLAN.md Adim 6.5). init_db ile senkron
-- (src/core/database.py); idempotent.
--
-- Neden: model birden fazla amacta (digest, report) kullanildiginda birini
-- guncelleyip digerini unutmak, bu refactor'un tam olarak ortadan kaldirmak
-- icin var oldugu CUSTOM_MODEL/CUSTOM_URL ayrismasini bir kat yukarida
-- yeniden uretiyordu. Tek ayar bu sinifi hatayi yapisal olarak imkansiz
-- kilar. Gozlemlenebilirlik AYRI bir eksen: token_usage.purpose kolonu
-- KORUNDU, bir cagrinin digest'ten mi rapordan mi geldigi hala gorunur.
--
-- Bu migration ayrica embedding'in kaldirilmasini (Adim 6.5.B) yansitir:
-- src/clients/embedding.py silindi, "embedding" artik gecerli bir amac
-- degil -- llm_settings zaten purpose tasimadigi icin bu migration'da ayrica
-- bir islem gerekmiyor (embedding kaldirimi PURPOSES sabitinde, kodda).
--
-- ``purpose TEXT PRIMARY KEY``'den ``id BOOLEAN PRIMARY KEY DEFAULT TRUE
-- CHECK (id)``'ye gecis birincil anahtar kolonunun kendisini degistiriyor --
-- bu ALTER TABLE ile yapilamaz, bu yuzden eski sekli tasiyan bir tablo DROP
-- edilip yeniden kuruluyor. Prod'a HIC DEPLOY EDILMEDI (REFACTOR_PLAN.md
-- Adim 7 henuz yapilmadi) -- veri kaybi riski yok.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'llm_settings' AND column_name = 'purpose'
    ) THEN
        DROP TABLE IF EXISTS llm_settings;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS llm_settings (
    id         BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    provider   TEXT NOT NULL REFERENCES llm_providers(provider),
    model      TEXT NOT NULL,
    params     JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT
);
