CREATE TABLE IF NOT EXISTS economy_rates (
    ticker TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (ticker, ts)
);
