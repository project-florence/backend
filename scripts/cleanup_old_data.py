"""Periyodik olarak eski mum verilerini temizler.

Retention kurallari:
  - 1m, 5m        -> 7 gun
  - 15m, 30m      -> 30 gun
  - 1h            -> 90 gun
  - 1d            -> 5 yil
  - 1wk, 1mo, 3mo -> silinmez (cok az satir)

In-process cron (`src.cron.tasks`) isinin thin CLI wrapper'idir.

Kullanım:
  python scripts/cleanup_old_data.py
"""

import asyncio

from src.cron.tasks import run_cleanup_old_data


if __name__ == "__main__":
    asyncio.run(run_cleanup_old_data())
