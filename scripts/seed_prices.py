"""Tüm BIST şirketlerinin fiyat verilerini çeker ve price_candles tablosuna yazar.

Tek seferlik kurulum scriptidir. 50'şerli batch'ler halinde yf.download() ile çeker.

Kullanım:
  python scripts/seed_prices.py [--batch 50] [--delay 10]
"""

import sys
import time
import argparse
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

import yfinance as yf
import pandas as pd
from src.core.config import init_config
from src.core.database import db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--delay", type=int, default=10)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=None)
    args = parser.parse_args()

    init_config()

    from src.services.bist import get_bist_companies_as_dict_from_redis
    companies = get_bist_companies_as_dict_from_redis()
    all_tickers = [c["ticker"] + ".IS" for c in companies]

    tickers = all_tickers[args.start:]
    if args.count:
        tickers = tickers[:args.count]

    total = len(tickers)
    batch_size = args.batch
    print(f"{total} ticker, {batch_size}'ser batch halinde çekiliyor...")

    inserted = 0
    for i in range(0, total, batch_size):
        batch = tickers[i : i + batch_size]
        batch_no = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        print(f"[{batch_no}/{total_batches}] {batch[0]} .. {batch[-1]}", end=" ")

        try:
            with open(os.devnull, "w") as devnull:
                with redirect_stderr(devnull), redirect_stdout(devnull):
                    df = yf.download(batch, period="5d", interval="1d", group_by="ticker", progress=False)

            if df is None or df.empty:
                print("→ veri yok")
                _maybe_sleep(i, total, batch_size, args.delay)
                continue
        except Exception as e:
            print(f"→ hata: {e}")
            _maybe_sleep(i, total, batch_size, args.delay)
            continue

        count = _process_dataframe(df, batch)
        inserted += count
        print(f"→ {count} kaydedildi")

        _maybe_sleep(i, total, batch_size, args.delay)

    print(f"\nTamamlandı. {inserted}/{total} ticker kaydedildi.")


def _process_dataframe(df: pd.DataFrame, batch_tickers: list[str]) -> int:
    multi = isinstance(df.columns, pd.MultiIndex)
    if multi:
        available = set(df.columns.levels[0])
    else:
        available = {batch_tickers[0]} if not df.empty else set()

    count = 0
    with db.cursor() as cur:
        for ticker in batch_tickers:
            if ticker not in available:
                continue
            try:
                tdf = df[ticker].dropna() if multi else df.dropna()
                if tdf.empty:
                    continue
                last = tdf.iloc[-1]
                ts = last.name.to_pydatetime() if isinstance(last.name, pd.Timestamp) else last.name

                cur.execute(
                    """INSERT INTO price_candles (ticker, interval, ts, open, high, low, close, volume)
                       VALUES (%s, '1d', %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (ticker, interval, ts)
                       DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high,
                                     low = EXCLUDED.low, close = EXCLUDED.close,
                                     volume = EXCLUDED.volume""",
                    (ticker, ts,
                     float(last["Open"]) if pd.notna(last["Open"]) else None,
                     float(last["High"]) if pd.notna(last["High"]) else None,
                     float(last["Low"]) if pd.notna(last["Low"]) else None,
                     float(last["Close"]) if pd.notna(last["Close"]) else None,
                     int(last["Volume"]) if pd.notna(last["Volume"]) else 0),
                )
                count += 1
            except Exception:
                continue
    db.commit()
    return count


def _maybe_sleep(i, total, batch_size, delay):
    if i + batch_size < total:
        time.sleep(delay)


if __name__ == "__main__":
    main()
