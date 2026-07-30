"""
Periyodik olarak eski mum verilerini temizler.
Her gun 03:00'te cron ile calismasi icin tasarlanmistir.

Retention kurallari:
  - 1m, 5m        -> 7 gun
  - 15m, 30m      -> 30 gun
  - 1h            -> 90 gun
  - 1d            -> 5 yil
  - 1wk, 1mo, 3mo -> silinmez (cok az satir)
"""

import sys
from datetime import datetime, timezone, timedelta
sys.path.insert(0, ".")

from src.core.database import db

RETENTION = {
    "1m": timedelta(days=7),
    "5m": timedelta(days=7),
    "15m": timedelta(days=30),
    "30m": timedelta(days=30),
    "1h": timedelta(days=90),
    "1d": timedelta(days=365 * 5),
}


def main():
    total_deleted = 0
    now = datetime.now(timezone.utc)

    for interval, max_age in RETENTION.items():
        cutoff = now - max_age
        with db.cursor() as cur:
            cur.execute(
                "DELETE FROM price_candles WHERE interval = %s AND ts < %s",
                (interval, cutoff),
            )
            deleted = cur.rowcount
        db.commit()
        if deleted:
            print(f"  {interval}: {deleted} satir silindi (before {cutoff.date()})")
            total_deleted += deleted

    if total_deleted:
        print(f"\nToplam {total_deleted} eski satir temizlendi.")
    else:
        print("Temizlenecek veri yok.")


if __name__ == "__main__":
    main()
