"""BIST şirketleri için stock vector'leri hesaplar ve Redis'e yazar.

In-process cron (`src.cron.tasks`) isinin thin CLI wrapper'idir.

Kullanım:
  python scripts/seed_vectors.py                        # popüler 200 şirket (cron varsayılanı)
  python scripts/seed_vectors.py --count 100            # popüler 100 şirket
  python scripts/seed_vectors.py --count -1             # tüm şirketler
  python scripts/seed_vectors.py --delay 5              # istekler arası saniye
"""

import argparse
import asyncio

from src.cron.tasks import run_seed_vectors


def main():
    parser = argparse.ArgumentParser(description="BIST şirketleri için stock vector'leri hesaplar ve Redis'e yazar")
    parser.add_argument("--count", type=int, default=200,
                        help="İşlenecek şirket sayısı (-1 = tümü, varsayılan: 200)")
    parser.add_argument("--delay", type=float, default=None,
                        help="İstekler arası bekleme saniyesi")
    args = parser.parse_args()

    asyncio.run(run_seed_vectors(count=args.count, delay=args.delay))


if __name__ == "__main__":
    main()
