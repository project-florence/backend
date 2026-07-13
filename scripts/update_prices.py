"""BIST şirketlerinin fiyat verilerini periyodik olarak günceller.

Cron ile kullanım için tasarlanmıştır. Üç kademeli güncelleme:
  - BIST30: her 10 dakikada bir
  - Popüler 157 şirket (mapping): her 1 saatte bir
  - Kalanlar: her 12 saatte bir

Kullanım:
  python scripts/update_prices.py              # tüm kademeleri kontrol eder
  python scripts/update_prices.py --tier bist30   # sadece BIST30
  python scripts/update_prices.py --tier popular  # sadece popüler
  python scripts/update_prices.py --tier rest     # sadece kalanlar
"""

import sys
import time
import argparse
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone, timedelta
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

import yfinance as yf
import pandas as pd
from src.core.config import init_config
from src.core.database import db
from src.utils.mapping import load_bist_mapping

BIST30_TICKERS = [
    "AKBNK", "ARCLK", "ASELS", "BIMAS", "CCOLA",
    "EKGYO", "ENKAI", "EREGL", "FROTO", "GARAN",
    "HALKB", "ISCTR", "KCHOL", "KRDMD", "MGROS",
    "PETKM", "PGSUS", "SAHOL", "SASA", "SISE",
    "TAVHL", "TCELL", "THYAO", "TOASO", "TTKOM",
    "TUPRS", "VAKBN", "YKBNK", "AEFES",
]

BATCH_SIZE = 50
BATCH_DELAY = 10


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=["bist30", "popular", "rest"], default=None,
                        help="Sadece belirli kademeyi güncelle")
    args = parser.parse_args()

    init_config()

    mapping = load_bist_mapping()
    popular_tickers = list(mapping.keys())

    from src.services.bist import get_bist_companies_as_dict_from_redis
    companies = get_bist_companies_as_dict_from_redis()
    all_tickers = {c["ticker"] for c in companies}

    bist30_set = set(BIST30_TICKERS)
    popular_set = set(popular_tickers) & all_tickers
    rest_set = all_tickers - bist30_set - popular_set

    now = datetime.now(timezone.utc)

    tiers = []

    if args.tier is None or args.tier == "bist30":
        tiers.append(("BIST30", list(bist30_set & all_tickers), timedelta(minutes=10)))
    if args.tier is None or args.tier == "popular":
        tiers.append(("POPÜLER", list(popular_set), timedelta(hours=1)))
    if args.tier is None or args.tier == "rest":
        tiers.append(("DİĞER", list(rest_set), timedelta(hours=12)))

    total_updated = 0
    for tier_name, ticker_list, interval in tiers:
        need_update = _needs_update(ticker_list, now, interval)
        if not need_update:
            print(f"[{tier_name}] Güncelleme gerektiren yok ({len(ticker_list)} ticker)")
            continue

        print(f"[{tier_name}] {len(need_update)}/{len(ticker_list)} ticker güncellenecek...")
        tickers_is = [t + ".IS" for t in need_update]

        for i in range(0, len(tickers_is), BATCH_SIZE):
            batch = tickers_is[i : i + BATCH_SIZE]
            _update_batch(batch, tier_name, i, len(tickers_is))
            total_updated += len(batch)

            if i + BATCH_SIZE < len(tickers_is):
                print(f"  {BATCH_DELAY}s bekleniyor...")
                time.sleep(BATCH_DELAY)

    print(f"\nToplam {total_updated} ticker güncellendi.")


def _needs_update(ticker_list: list[str], now: datetime, max_age: timedelta) -> list[str]:
    if not ticker_list:
        return []

    placeholders = ",".join(["%s"] * len(ticker_list))
    tickers_is = [t + ".IS" for t in ticker_list]

    with db.cursor() as cur:
        cur.execute(
            f"""SELECT ticker, MAX(ts) as last_ts
                FROM price_candles
                WHERE ticker IN ({placeholders}) AND interval = '1d'
                GROUP BY ticker""",
            tickers_is,
        )
        rows = cur.fetchall()
        last_ts_map = {r[0]: r[1] for r in rows}

    need = []
    for t, t_is in zip(ticker_list, tickers_is):
        last_ts = last_ts_map.get(t_is)
        if last_ts is None or (now - last_ts) > max_age:
            need.append(t)

    return need


def _update_batch(batch_tickers: list[str], tier_name: str, offset: int, total: int):
    print(f"  {tier_name} [{offset + 1}-{offset + len(batch_tickers)}/{total}] indiriliyor...", end=" ")

    try:
        with open(os.devnull, "w") as devnull:
            with redirect_stderr(devnull), redirect_stdout(devnull):
                df = yf.download(batch_tickers, period="5d", interval="1d", group_by="ticker", progress=False)

        if df is None or df.empty:
            print("veri yok")
            return
    except Exception as e:
        print(f"hata: {e}")
        return

    multi = isinstance(df.columns, pd.MultiIndex)
    if multi:
        available = set(df.columns.levels[0])
    else:
        available = {batch_tickers[0]} if not df.empty else set()

    count = 0
    with db.cursor() as cur:
        for ticker in batch_tickers:
            if ticker not in available:
                continue
            try:
                tdf = df[ticker].dropna() if multi else df.dropna()
                if tdf.empty:
                    continue
                last = tdf.iloc[-1]
                ts = last.name.to_pydatetime() if isinstance(last.name, pd.Timestamp) else last.name

                cur.execute(
                    """INSERT INTO price_candles (ticker, interval, ts, open, high, low, close, volume)
                       VALUES (%s, '1d', %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (ticker, interval, ts)
                       DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high,
                                     low = EXCLUDED.low, close = EXCLUDED.close,
                                     volume = EXCLUDED.volume""",
                    (ticker, ts,
                     float(last["Open"]) if pd.notna(last["Open"]) else None,
                     float(last["High"]) if pd.notna(last["High"]) else None,
                     float(last["Low"]) if pd.notna(last["Low"]) else None,
                     float(last["Close"]) if pd.notna(last["Close"]) else None,
                     int(last["Volume"]) if pd.notna(last["Volume"]) else 0),
                )
                count += 1
            except Exception:
                continue
    db.commit()
    print(f"{count} kaydedildi")


if __name__ == "__main__":
    main()
