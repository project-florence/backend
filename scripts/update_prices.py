"""BIST şirketlerinin fiyat verilerini periyodik olarak günceller.

Cron ile kullanım için tasarlanmıştır. Üç kademeli güncelleme:
  - BIST30: her 10 dakikada bir, interval=5m
  - Popüler 157 şirket (mapping): her 1 saatte bir, interval=30m
  - Kalanlar: her 12 saatte bir, interval=1d

Bu script artik in-process cron (`src.cron.tasks`) uzerinden calisan islerin
thin CLI wrapper'idir.

Kullanım:
  python scripts/update_prices.py                          # tüm kademeleri çalıştırır
  python scripts/update_prices.py --tier bist30            # sadece BIST30
  python scripts/update_prices.py --tier popular --info    # popüler fiyat + profil
  python scripts/update_prices.py --no-lock                # debug için lock'suz
"""

import argparse
import asyncio

from src.cron.tasks import TIERS, update_tier


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=list(TIERS.keys()), default=None,
                        help="Sadece belirli kademeyi guncelle")
    parser.add_argument("--interval", default=None,
                        help="Candle interval (5m, 30m, 1h, 1d). Varsayilan: kademeye gore")
    parser.add_argument("--info", action="store_true",
                        help="Popular kademesinde fiyat guncellemesi sonrasi profil de tazelenir")
    parser.add_argument("--no-lock", action="store_true",
                        help="Redis lock'u atla (debug icin)")
    args = parser.parse_args()

    tiers = [args.tier] if args.tier else list(TIERS.keys())
    total_updated = 0
    for tier in tiers:
        total_updated += await update_tier(tier, interval=args.interval, info=args.info, no_lock=args.no_lock)

    print(f"\nToplam {total_updated} ticker guncellendi.")


if __name__ == "__main__":
    asyncio.run(main())
