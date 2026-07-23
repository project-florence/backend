"""Tüm kullanıcıların kredilerini günlük olarak yeniler.

Cron ile gece 00:00'da çalışacak şekilde tasarlanmıştır.
Her kullanıcıya 5 kredi ekler.

Kullanım:
  python scripts/credit_refiller.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import init_config
from src.core.database import db


def refill_credits():
    init_config()

    with db.cursor() as cur:
        try:
            cur.execute("""
                UPDATE users
                SET credits = credits + 5
            """)
            db.commit()
            print(f"Krediler yenilendi. Etkilenen kullanıcı: {cur.rowcount}")
        except Exception as e:
            db.rollback()
            print(f"Hata: {e}")
            sys.exit(1)


if __name__ == "__main__":
    refill_credits()
