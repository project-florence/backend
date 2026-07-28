"""Tum populer hisseler icin stock vector'leri hesaplar ve Redis + DB'ye kaydeder.

Kullanim:
  python scripts/compute_vectors.py [--top 200] [--all]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.database import db, init_db
from core.config import init_config

init_config()
init_db()


def main(top_n: int = 200, all_tickers: bool = False):
    from src.services.stats import get_popular_tickers
    from src.services.company import get_company_info
    from src.analysis.stock_vector import company_vector, write_vectors_to_redis, _write_to_db

    if all_tickers:
        from src.services.bist import get_bist_tickers_as_dict_from_redis
        tickers = list(get_bist_tickers_as_dict_from_redis().keys())
    else:
        tickers = get_popular_tickers(top_n)

    total = len(tickers)
    print(f"Computing vectors for {total} tickers...")

    count = 0
    batch = []
    for i, ticker in enumerate(tickers):
        profile = get_company_info(ticker)
        if profile:
            vec = company_vector(profile)
            entry = {"ticker": ticker, **vec}
            batch.append(entry)
            _write_to_db(ticker, vec)
            count += 1
        else:
            print(f"  [{i+1}/{total}] SKIP {ticker} (no data)")

        if len(batch) >= 50:
            write_vectors_to_redis(batch)
            batch = []

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{total}] {count} computed so far...")
            time.sleep(0.5)

    if batch:
        write_vectors_to_redis(batch)

    print(f"Done: {count}/{total} vectors computed and saved.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Precompute stock vectors")
    parser.add_argument("--top", type=int, default=200, help="Number of top tickers to process")
    parser.add_argument("--all", action="store_true", help="Process ALL BIST tickers")
    args = parser.parse_args()
    main(top_n=args.top, all_tickers=args.all)
