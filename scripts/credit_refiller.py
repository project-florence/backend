"""Tüm kullanıcıların free credit'lerini günlük olarak yeniler (cap'li).

Cron ile gece 00:00'da çalışacak şekilde tasarlanmıştır.
Her kullanıcıya FREE_CREDIT_MAX üst sınırını geçmeyecek kadar free credit ekler.

Kullanım:
  python scripts/credit_refiller.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.credits import daily_refill


if __name__ == "__main__":
    try:
        count = daily_refill()
        print(f"Free credits yenilendi. Etkilenen kullanıcı: {count}")
    except Exception as e:
        print(f"Hata: {e}")
        sys.exit(1)
