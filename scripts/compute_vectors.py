"""Tum populer hisseler icin stock vector'leri hesaplar ve Redis + DB'ye kaydeder.

Kullanim:
  python scripts/compute_vectors.py [--top 200] [--all]
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import init_config
from src.core.database import init_db


async def main(top_n: int = 200, all_tickers: bool = False):
    from src.analysis.stock_vector import company_vector, write_vectors_to_redis, _write_to_db
    from src.services.bist import get_bist_tickers_as_dict_from_redis
    from src.services.company import get_company_info
    from src.services.stats import get_popular_tickers

    if all_tickers:
        tickers = list((await get_bist_tickers_as_dict_from_redis()).keys())
    else:
        tickers = await get_popular_tickers(top_n)

    total = len(tickers)
    print(f"Computing vectors for {total} tickers...")

    count = 0
    batch = []
    for i, ticker in enumerate(tickers):
        profile = await get_company_info(ticker)
        if profile:
            vec = company_vector(profile)
            entry = {"ticker": ticker, **vec}
            batch.append(entry)
            await _write_to_db(ticker, vec)
            count += 1
        else:
            print(f"  [{i+1}/{total}] SKIP {ticker} (no data)")

        if len(batch) >= 50:
            await write_vectors_to_redis(batch)
            batch = []

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{total}] {count} computed so far...")
            await asyncio.sleep(0.5)

    if batch:
        await write_vectors_to_redis(batch)

    print(f"Done: {count}/{total} vectors computed and saved.")


if __name__ == "__main__":
    init_config()
    asyncio.run(init_db())

    parser = argparse.ArgumentParser(description="Precompute stock vectors")
    parser.add_argument("--top", type=int, default=200, help="Number of top tickers to process")
    parser.add_argument("--all", action="store_true", help="Process ALL BIST tickers")
    args = parser.parse_args()
    asyncio.run(main(top_n=args.top, all_tickers=args.all))
