-- economy_rates.price DOUBLE PRECISION → JSONB
-- JSON dict (Buying, Selling, Change) store edebilmek için

ALTER TABLE economy_rates ALTER COLUMN price TYPE JSONB USING to_jsonb(price::text);
