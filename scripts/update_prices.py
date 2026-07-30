"""BIST şirketlerinin fiyat verilerini periyodik olarak günceller.

Cron ile kullanım için tasarlanmıştır. Üç kademeli güncelleme:
  - BIST30: her 10 dakikada bir, interval=5m
  - Popüler 157 şirket (mapping): her 1 saatte bir, interval=30m
  - Kalanlar: her 12 saatte bir, interval=1d

Kullanım:
  python scripts/update_prices.py                          # tüm kademeleri kontrol eder
  python scripts/update_prices.py --tier bist30             # sadece BIST30
  python scripts/update_prices.py --tier popular            # sadece popüler
  python scripts/update_prices.py --tier rest               # sadece kalanlar
  python scripts/update_prices.py --tier bist30 --interval 1d  # belirli interval
  python scripts/update_prices.py --info                    # fiyat + company info
  python scripts/update_prices.py --tier popular --info     # sadece popular'in fiyat + info'su
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
from src.core.database import db
from src.core.redis import r
from src.utils.mapping import load_bist_mapping
from src.services.company import get_company_info
from src.services.price import INTRADAY_INTERVALS, invalidate_price_cache

BIST30_TICKERS = [
    "AKBNK", "ARCLK", "ASELS", "BIMAS", "CCOLA",
    "EKGYO", "ENKAI", "EREGL", "FROTO", "GARAN",
    "HALKB", "ISCTR", "KCHOL", "KRDMD", "MGROS",
    "PETKM", "PGSUS", "SAHOL", "SASA", "SISE",
    "TAVHL", "TCELL", "THYAO", "TOASO", "TTKOM",
    "TUPRS", "VAKBN", "YKBNK", "AEFES",
]

BATCH_DELAY = 10
INFO_TICKER_DELAY = 5

# Tier config: (name, tickers, cron_frequency, default_interval, batch_size)
TIERS = {
    "bist30": ("BIST30", timedelta(minutes=10), "5m", 15),
    "popular": ("POPÜLER", timedelta(hours=1), "30m", 20),
    "rest": ("DİĞER", timedelta(hours=12), "1d", 50),
}


def _acquire_cron_lock(tier_name: str, ttl: int = 600) -> bool:
    lock_key = f"lock:cron:{tier_name}"
    acquired = r.set(lock_key, "1", ex=ttl, nx=True)
    return acquired is not None


def _release_cron_lock(tier_name: str):
    r.delete(f"lock:cron:{tier_name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=list(TIERS.keys()), default=None,
                        help="Sadece belirli kademeyi güncelle")
    parser.add_argument("--interval", default=None,
                        help="Candle interval (5m, 30m, 1h, 1d). Varsayılan: kademeye göre")
    parser.add_argument("--info", action="store_true",
                        help="Fiyat güncellemesinden sonra company info'yu da tazele (popular kademesi, 5s aralikla)")
    parser.add_argument("--no-lock", action="store_true",
                        help="Redis lock'u atla (debug için)")
    args = parser.parse_args()

    mapping = load_bist_mapping()
    popular_tickers = list(mapping.keys())

    from src.services.bist import get_bist_companies_as_dict_from_redis
    companies = get_bist_companies_as_dict_from_redis()
    all_tickers = {c["ticker"] for c in companies}

    bist30_set = set(BIST30_TICKERS)
    popular_set = set(popular_tickers) & all_tickers
    rest_set = all_tickers - bist30_set - popular_set

    ticker_sets = {
        "bist30": list(bist30_set & all_tickers),
        "popular": list(popular_set),
        "rest": list(rest_set),
    }

    now = datetime.now(timezone.utc)

    tier_keys = [args.tier] if args.tier else list(TIERS.keys())
    total_updated = 0

    for key in tier_keys:
        name, freq, default_interval, batch_size = TIERS[key]
        interval = args.interval or default_interval
        ticker_list = ticker_sets[key]

        if not args.no_lock and not _acquire_cron_lock(key):
            print(f"[{name}] {interval} — lock alinamadi (baskasi calisiyor), atlaniyor")
            continue

        try:
            need_update = _needs_update(ticker_list, now, freq, interval)
            if not need_update:
                print(f"[{name}] {interval} — güncelleme gerektiren yok ({len(ticker_list)} ticker)")
                continue

            print(f"[{name}] {interval} — {len(need_update)}/{len(ticker_list)} ticker güncellenecek...")
            tickers_is = [t + ".IS" for t in need_update]

            period = "5d" if interval in INTRADAY_INTERVALS else "5d"
            for i in range(0, len(tickers_is), batch_size):
                batch = tickers_is[i : i + batch_size]
                _update_batch(batch, interval, period, name, i, len(tickers_is))
                total_updated += len(batch)

                if i + batch_size < len(tickers_is):
                    print(f"  {BATCH_DELAY}s bekleniyor...")
                    time.sleep(BATCH_DELAY)
        finally:
            _release_cron_lock(key)

    if args.info:
        _refresh_company_info(tier_keys)

    print(f"\nToplam {total_updated} ticker güncellendi.")


def _needs_update(ticker_list: list[str], now: datetime, max_age: timedelta, interval: str = "1d") -> list[str]:
    if not ticker_list:
        return []

    placeholders = ",".join(["%s"] * len(ticker_list))
    tickers_is = [t + ".IS" for t in ticker_list]

    with db.cursor() as cur:
        cur.execute(
            f"""SELECT ticker, MAX(ts) as last_ts
                FROM price_candles
                WHERE ticker IN ({placeholders}) AND interval = %s
                GROUP BY ticker""",
            tickers_is + [interval],
        )
        rows = cur.fetchall()
        last_ts_map = {r[0]: r[1] for r in rows}

    need = []
    for t, t_is in zip(ticker_list, tickers_is):
        last_ts = last_ts_map.get(t_is)
        if last_ts is None or (now - last_ts) > max_age:
            need.append(t)

    return need


def _update_batch(batch_tickers: list[str], interval: str, period: str, tier_name: str, offset: int, total: int):
    print(f"  {tier_name} [{offset + 1}-{offset + len(batch_tickers)}/{total}] {interval} indiriliyor...", end=" ")

    try:
        with open(os.devnull, "w") as devnull:
            with redirect_stderr(devnull), redirect_stdout(devnull):
                df = yf.download(batch_tickers, period=period, interval=interval, group_by="ticker", progress=False)

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
                tdf = df[ticker] if multi else df
                tdf = tdf.dropna(how="all")
                if tdf.empty:
                    continue

                for ts, row in tdf.iterrows():
                    ts_dt = ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts
                    cur.execute(
                        """INSERT INTO price_candles (ticker, interval, ts, open, high, low, close, volume)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (ticker, interval, ts)
                           DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high,
                                         low = EXCLUDED.low, close = EXCLUDED.close,
                                         volume = EXCLUDED.volume""",
                        (ticker, interval, ts_dt,
                         float(row["Open"]) if pd.notna(row["Open"]) else None,
                         float(row["High"]) if pd.notna(row["High"]) else None,
                         float(row["Low"]) if pd.notna(row["Low"]) else None,
                         float(row["Close"]) if pd.notna(row["Close"]) else None,
                         int(row["Volume"]) if pd.notna(row["Volume"]) else 0),
                    )
                    count += 1
            except Exception:
                continue
    db.commit()

    updated_tickers = [t for t in batch_tickers if t in available]
    for ticker in updated_tickers:
        invalidate_price_cache(ticker, interval)

    print(f"{count} mum kaydedildi, {len(updated_tickers)} cache invalidated")


def _refresh_company_info(tier_keys: list[str]):
    print("\n[INFO] Company info tazeleniyor...")

    if "popular" not in tier_keys:
        print("[INFO] Popular kademesi istenmemis, atlaniyor.")
        return

    mapping = load_bist_mapping()
    popular_tickers = list(mapping.keys())

    total = len(popular_tickers)
    for i, ticker in enumerate(popular_tickers, 1):
        print(f"  [{i}/{total}] {ticker}...", end=" ", flush=True)
        try:
            profile = get_company_info(ticker, use_cache=False)
            if profile:
                print(f"OK ({len(profile)} alan)")
            else:
                print("veri yok")
        except Exception as e:
            print(f"hata: {e}")

        if i < total:
            time.sleep(INFO_TICKER_DELAY)

    print("[INFO] Company info tazeleme tamamlandi.")


if __name__ == "__main__":
    main()
