"""BIST şirketleri için stock vector'leri hesaplar ve Redis'e yazar.

Tek seferlik kurulum veya periyodik güncelleme için kullanılır.
Vector'ler `stock_vector:<ticker>` anahtarıyla Redis'te 24 saat (TTL) saklanır.

Kullanım:
  python scripts/seed_vectors.py                        # popüler 100 şirket
  python scripts/seed_vectors.py --count 200            # popüler 200 şirket
  python scripts/seed_vectors.py --count -1             # tüm şirketler
  python scripts/seed_vectors.py --delay 5              # istekler arası saniye (varsayılan: config'deki rate_limit_delay)
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import init_config
from src.core.redis import r as redis_client
from src.services.company import get_company_info
from src.services.stats import get_all_stats
from src.analysis.stock_vector import company_vector, write_vectors_to_redis


def main():
    parser = argparse.ArgumentParser(description="BIST şirketleri için stock vector'leri hesaplar ve Redis'e yazar")
    parser.add_argument("--count", type=int, default=100,
                        help="İşlenecek şirket sayısı (-1 = tümü, varsayılan: 100)")
    parser.add_argument("--delay", type=float, default=None,
                        help="İstekler arası bekleme saniyesi (varsayılan: config'deki rate_limit_delay)")
    args = parser.parse_args()

    init_config()

    stats = get_all_stats()
    all_tickers = [s["ticker"] for s in stats]

    if args.count == -1:
        tickers = all_tickers
    else:
        tickers = all_tickers[:args.count]

    total = len(tickers)
    print(f"{total} şirket için vector hesaplanıyor...")

    vectors = []
    errors = 0

    for i, ticker in enumerate(tickers, 1):
        print(f"  [{i}/{total}] {ticker}...", end=" ", flush=True)

        try:
            profile = get_company_info(ticker, use_cache=True)
            if not profile:
                print("profil verisi yok")
                errors += 1
                continue

            vec = company_vector(profile)
            vectors.append({"ticker": ticker, **vec})
            print(f"risk={vec['risk']:.2f} horizon={vec['horizon']:.2f} profitability={vec['profitability']:.2f}")

        except Exception as e:
            print(f"hata: {e}")
            errors += 1

        batch_size = 50
        if len(vectors) >= batch_size:
            write_vectors_to_redis(vectors)
            print(f"    → {len(vectors)} vector Redis'e yazıldı")
            vectors = []

        if args.delay is not None and i < total:
            time.sleep(args.delay)

    if vectors:
        write_vectors_to_redis(vectors)
        print(f"  → {len(vectors)} vector Redis'e yazıldı")

    written = total - errors
    print(f"\nTamamlandı. {written}/{total} şirket başarıyla vektörize edildi.")
    if errors:
        print(f"  Hatalar: {errors}")


if __name__ == "__main__":
    main()
