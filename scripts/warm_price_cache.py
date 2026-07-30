"""
Pre-warm Redis cache for all BIST tickers in parallel.
Warms 5y 1d price history so frontend gets instant responses.
Run daily via cron.
"""

import json
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, ".")

from src.services.price import get_price_history
from src.services.bist import get_bist_tickers_as_json_from_redis


def _warm_one(ticker: str) -> None:
    get_price_history(ticker, "5y", "1d", hot=True)


def main():
    tickers = json.loads(get_bist_tickers_as_json_from_redis())
    print(f"Warming {len(tickers)} tickers with 10 parallel workers...")

    start_time = time.time()
    done = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for raw in tickers:
            ticker = raw if raw.endswith(".IS") else f"{raw}.IS"
            future = executor.submit(_warm_one, ticker)
            futures[future] = ticker

        for future in as_completed(futures):
            ticker = futures[future]
            done += 1
            try:
                future.result()
                print(f"[{done}/{len(tickers)}] OK  {ticker}")
            except Exception as e:
                errors += 1
                print(f"[{done}/{len(tickers)}] ERR {ticker}: {e}")

            if done % 10 == 0:
                elapsed = time.time() - start_time
                rate = done / elapsed
                remaining = (len(tickers) - done) / rate
                print(f"  --- {done}/{len(tickers)} done, {rate:.1f} tickers/s, ~{remaining:.0f}s remaining ---")

    elapsed = time.time() - start_time
    print(f"\nDone. {len(tickers)} tickers warmed in {elapsed:.0f}s ({elapsed/60:.1f} min), {errors} errors")


if __name__ == "__main__":
    main()
