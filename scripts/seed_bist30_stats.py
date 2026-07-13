"""BIST30 şirketlerine manuel başlangıç istatistiği verir.

Sunucuda (uvicorn --workers N) ÇALIŞTIRMA, tek seferlik kurulum scriptidir.
Organik kullanıcı olmadığında popüler sıralamada BIST30'ların önde
görünmesini sağlar.

Kullanım:
  python scripts/seed_bist30_stats.py [--boost N]
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import init_config
from src.core.database import db
from src.services.bist import get_bist_companies_as_dict_from_redis

BIST30_TICKERS = [
    "AKBNK", "ARCLK", "ASELS", "BIMAS", "EKGYO", "ENKAI",
    "EREGL", "FROTO", "GARAN", "HALKB", "ISCTR", "KCHOL",
    "KRDMD", "MGROS", "PETKM", "PGSUS", "SAHOL", "SASA",
    "SISE", "TAVHL", "TCELL", "THYAO", "TOASO", "TTKOM",
    "TUPRS", "VAKBN", "YKBNK", "AEFES", "CCOLA",
]

STAT_FIELDS = [
    "info_count",
    "report_count",
    "news_count",
    "history_count",
    "simulation_count",
    "favorite_count",
]


def seed(boost: int = 50):
    init_config()
    companies = get_bist_companies_as_dict_from_redis()
    existing_tickers = {c["ticker"] for c in companies}

    with db.cursor() as cur:
        for ticker in BIST30_TICKERS:
            if ticker not in existing_tickers:
                print(f"  Atlandı (bulunamadı): {ticker}")
                continue

            cur.execute(
                f"""INSERT INTO ticker_stats (ticker, {', '.join(STAT_FIELDS)}, updated_at)
                    VALUES (%s, {', '.join([str(boost)] * len(STAT_FIELDS))}, NOW())
                    ON CONFLICT (ticker)
                    DO UPDATE SET
                        {', '.join(f'{f} = ticker_stats.{f} + {boost}' for f in STAT_FIELDS)},
                        updated_at = NOW()""",
                (ticker,),
            )
            print(f"  {ticker}: +{boost} her stat")
        db.commit()

    print(f"\nTamamlandı. {len(BIST30_TICKERS)} BIST30 şirketine +{boost} eklendi.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BIST30 şirketlerine başlangıç istatistiği verir")
    parser.add_argument("--boost", type=int, default=50, help="Her istatistiğe eklenecek değer (varsayılan: 50)")
    args = parser.parse_args()
    seed(boost=args.boost)
