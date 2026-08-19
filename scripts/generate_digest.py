"""Gunluk piyasa bultenini (market digest) belirli bir slot icin uretir.

In-process cron (`src.cron.tasks.run_market_digest`) isinin thin CLI
wrapper'idir: bulteni uretir, Redis'e yazar ve digests tablosuna kaydeder.

Kullanim:
  python scripts/generate_digest.py                  # evening (varsayilan)
  python scripts/generate_digest.py morning          # morning | noon | evening
"""

import asyncio
import os
import sys

# Repo convention (scripts/backfill_fx_candles.py pattern): make ``src``
# importable when the script runs directly (sys.path[0] is scripts/ otherwise).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.services.digest import generate_digest


def main() -> None:
    slot = sys.argv[1] if len(sys.argv) > 1 else "evening"
    digest = asyncio.run(generate_digest(slot))
    print(f"title: {digest.title}")
    print(f"id:    {digest.id}")
    print(f"slot:  {digest.slot}  date: {digest.date}")


if __name__ == "__main__":
    main()