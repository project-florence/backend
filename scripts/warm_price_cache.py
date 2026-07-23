"""
Pre-warm Redis cache for all BIST tickers.
Warms 5y 1d price history so frontend gets instant responses.
Run daily via cron.
"""

import json
import time
import sys
sys.path.insert(0, ".")

from src.services.price import get_price_history
from src.services.bist import get_bist_tickers_as_json_from_redis


def main():
    tickers = json.loads(get_bist_tickers_as_json_from_redis())
    print(f"Warming {len(tickers)} tickers...")

    start_time = time.time()
    for i, raw in enumerate(tickers, 1):
        ticker = raw if raw.endswith(".IS") else f"{raw}.IS"
        try:
            get_price_history(ticker, "5y", "1d", hot=True)
            print(f"[{i}/{len(tickers)}] OK  {ticker}")
        except Exception as e:
            print(f"[{i}/{len(tickers)}] ERR {ticker}: {e}")

        if i % 10 == 0:
            elapsed = time.time() - start_time
            rate = i / elapsed
            remaining = (len(tickers) - i) / rate
            print(f"  --- {i}/{len(tickers)} done, {rate:.1f} tickers/s, ~{remaining:.0f}s remaining ---")

    elapsed = time.time() - start_time
    print(f"\nDone. {len(tickers)} tickers warmed in {elapsed:.0f}s ({elapsed/60:.1f} min)")


if __name__ == "__main__":
    main()
