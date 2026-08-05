"""Tüm kullanıcıların free credit'lerini günlük olarak yeniler (cap'li).

In-process cron (`src.cron.tasks`) isinin thin CLI wrapper'idir.

Kullanım:
  python scripts/credit_refiller.py
"""

from src.cron.tasks import run_credit_refill


if __name__ == "__main__":
    run_credit_refill()
