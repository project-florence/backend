"""Pre-warm Redis cache for all BIST tickers in parallel.

Warms 5y 1d price history so frontend gets instant responses.

In-process cron (`src.cron.tasks`) isinin thin CLI wrapper'idir.

Kullanım:
  python scripts/warm_price_cache.py
"""

import asyncio

from src.cron.tasks import run_warm_price_cache


if __name__ == "__main__":
    asyncio.run(run_warm_price_cache())
