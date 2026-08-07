"""Tüm BIST şirketlerinin fiyat verilerini çeker ve price_candles tablosuna yazar.

Tek seferlik kurulum scriptidir. 50'şer batch'ler halinde yf.download() ile çeker.

Kullanım:
  python scripts/seed_prices.py [--batch 50] [--delay 10]
"""

import argparse
import asyncio
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import yfinance as yf

from src.core.database import db


async def _download(batch, period="5d", interval="1d"):
    def _do():
        with open(os.devnull, "w") as devnull:
            with redirect_stderr(devnull), redirect_stdout(devnull):
                return yf.download(batch, period=period, interval=interval, group_by="ticker", progress=False)
    return await asyncio.to_thread(_do)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--delay", type=int, default=10)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=None)
    args = parser.parse_args()

    from src.services.bist import get_bist_companies_as_dict_from_redis
    companies = await get_bist_companies_as_dict_from_redis()
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
            df = await _download(batch)
            if df is None or df.empty:
                print("→ veri yok")
                await _maybe_sleep(i, total, batch_size, args.delay)
                continue
        except Exception as e:
            print(f"→ hata: {e}")
            await _maybe_sleep(i, total, batch_size, args.delay)
            continue

        count = await _process_dataframe(df, batch)
        inserted += count
        print(f"→ {count} kaydedildi")

        await _maybe_sleep(i, total, batch_size, args.delay)

    print(f"\nTamamlandı. {inserted}/{total} ticker kaydedildi.")


async def _process_dataframe(df: pd.DataFrame, batch_tickers: list[str]) -> int:
    multi = isinstance(df.columns, pd.MultiIndex)
    if multi:
        available = set(df.columns.levels[0])
    else:
        available = {batch_tickers[0]} if not df.empty else set()

    count = 0
    async with db.cursor(row_factory=None) as cur:
        for ticker in batch_tickers:
            if ticker not in available:
                continue
            try:
                tdf = df[ticker].dropna() if multi else df.dropna()
                if tdf.empty:
                    continue
                last = tdf.iloc[-1]
                ts = last.name.to_pydatetime() if isinstance(last.name, pd.Timestamp) else last.name

                await cur.execute(
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
    await db.commit()
    return count


async def _maybe_sleep(i, total, batch_size, delay):
    if i + batch_size < total:
        await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
