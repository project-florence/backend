"""Periyodik islerin cagrilabilir fonksiyonlari.

Bu fonksiyonlar hem `scripts/` altindaki CLI wrapper'larindan hem de
`src/cron/register.py` uzerinden in-process cron tarafindan calistirilir.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from src.analysis.stock_vector import company_vector, write_vectors_to_redis
from src.core.database import db
from src.core.redis import r
from src.services.bist import get_bist_companies_as_dict_from_redis
from src.services.company import get_company_info
from src.services.price import INTRADAY_INTERVALS, get_price_history, invalidate_price_cache
from src.services.stats import get_all_stats
from src.utils.mapping import load_bist_mapping

logger = logging.getLogger(__name__)

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

# Tier config: (name, cron_frequency, default_interval, batch_size)
TIERS = {
    "bist30": ("BIST30", timedelta(minutes=10), "5m", 15),
    "popular": ("POPÜLER", timedelta(hours=1), "30m", 20),
    "rest": ("DİĞER", timedelta(hours=12), "1d", 50),
}


# ----------------------------------------------------------------------
# Fiyat guncelleme
# ----------------------------------------------------------------------
def _acquire_cron_lock(tier_name: str, ttl: int = 600) -> bool:
    lock_key = f"lock:cron:{tier_name}"
    acquired = r.set(lock_key, "1", ex=ttl, nx=True)
    return acquired is not None


def _release_cron_lock(tier_name: str) -> None:
    r.delete(f"lock:cron:{tier_name}")


def _ticker_sets() -> dict[str, list[str]]:
    mapping = load_bist_mapping()
    popular_tickers = list(mapping.keys())
    companies = get_bist_companies_as_dict_from_redis()
    all_tickers = {c["ticker"] for c in companies}

    bist30_set = set(BIST30_TICKERS)
    popular_set = set(popular_tickers) & all_tickers
    rest_set = all_tickers - bist30_set - popular_set

    return {
        "bist30": list(bist30_set & all_tickers),
        "popular": list(popular_set),
        "rest": list(rest_set),
    }


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


def _update_batch(batch_tickers: list[str], interval: str, period: str, tier_name: str, offset: int, total: int) -> None:
    logger.info("  %s [%s-%s/%s] %s indiriliyor...", tier_name, offset + 1, offset + len(batch_tickers), total, interval)

    try:
        with open("/dev/null", "w") as devnull:
            with redirect_stderr(devnull), redirect_stdout(devnull):
                df = yf.download(batch_tickers, period=period, interval=interval, group_by="ticker", progress=False)

        if df is None or df.empty:
            logger.info("veri yok")
            return
    except Exception as e:
        logger.warning("indirme hatasi: %s", e)
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

    logger.info("%s mum kaydedildi, %s cache invalidated", count, len(updated_tickers))


def _refresh_company_info(tier_keys: list[str]) -> None:
    logger.info("[INFO] Company info tazeleniyor...")

    if "popular" not in tier_keys:
        logger.info("[INFO] Popular kademesi istenmemis, atlaniyor.")
        return

    mapping = load_bist_mapping()
    popular_tickers = list(mapping.keys())

    total = len(popular_tickers)
    for i, ticker in enumerate(popular_tickers, 1):
        logger.info("  [%s/%s] %s...", i, total, ticker)
        try:
            profile = get_company_info(ticker, use_cache=False)
            if profile:
                logger.info("OK (%s alan)", len(profile))
            else:
                logger.info("veri yok")
        except Exception as e:
            logger.warning("hata: %s", e)

        if i < total:
            time.sleep(INFO_TICKER_DELAY)

    logger.info("[INFO] Company info tazeleme tamamlandi.")


def update_tier(tier_name: str, interval: str | None = None, info: bool = False, no_lock: bool = False) -> int:
    if tier_name not in TIERS:
        raise ValueError(f"Bilinmeyen tier: {tier_name}")

    name, freq, default_interval, batch_size = TIERS[tier_name]
    effective_interval = interval or default_interval
    ticker_list = _ticker_sets()[tier_name]

    if not no_lock and not _acquire_cron_lock(tier_name):
        logger.info("[%s] %s — lock alinamadi (baskasi calisiyor), atlaniyor", name, effective_interval)
        return 0

    total_updated = 0
    try:
        now = datetime.now(timezone.utc)
        need_update = _needs_update(ticker_list, now, freq, effective_interval)
        if not need_update:
            logger.info("[%s] %s — guncelleme gerektiren yok (%s ticker)", name, effective_interval, len(ticker_list))
        else:
            logger.info("[%s] %s — %s/%s ticker guncellenecek...", name, effective_interval, len(need_update), len(ticker_list))
            tickers_is = [t + ".IS" for t in need_update]
            period = "5d" if effective_interval in INTRADAY_INTERVALS else "5d"
            for i in range(0, len(tickers_is), batch_size):
                batch = tickers_is[i: i + batch_size]
                _update_batch(batch, effective_interval, period, name, i, len(tickers_is))
                total_updated += len(batch)

                if i + batch_size < len(tickers_is):
                    logger.info("  %ss bekleniyor...", BATCH_DELAY)
                    time.sleep(BATCH_DELAY)
    finally:
        _release_cron_lock(tier_name)

    if info and tier_name == "popular":
        _refresh_company_info([tier_name])

    logger.info("Toplam %s ticker guncellendi.", total_updated)
    return total_updated


def run_update_bist30() -> None:
    update_tier("bist30")


def run_update_popular() -> None:
    update_tier("popular", info=True)


def run_update_rest() -> None:
    update_tier("rest")


# ----------------------------------------------------------------------
# Kredi dolumu
# ----------------------------------------------------------------------
def run_credit_refill() -> None:
    from src.services.credits import daily_refill

    count = daily_refill()
    logger.info("Free credits yenilendi. Etkilenen kullanici: %s", count)


# ----------------------------------------------------------------------
# Stock vector hesaplama
# ----------------------------------------------------------------------
def run_seed_vectors(count: int = 200, delay: float | None = None) -> None:
    stats = get_all_stats()
    all_tickers = [s["ticker"] for s in stats]

    if count == -1:
        tickers = all_tickers
    else:
        tickers = all_tickers[:count]

    total = len(tickers)
    logger.info("%s sirket icin vector hesaplaniyor...", total)

    vectors = []
    errors = 0

    for i, ticker in enumerate(tickers, 1):
        logger.info("  [%s/%s] %s...", i, total, ticker)
        try:
            profile = get_company_info(ticker, use_cache=True)
            if not profile:
                logger.info("profil verisi yok")
                errors += 1
                continue

            vec = company_vector(profile)
            vectors.append({"ticker": ticker, **vec})
            logger.info("risk=%.2f horizon=%.2f profitability=%.2f", vec["risk"], vec["horizon"], vec["profitability"])
        except Exception as e:
            logger.warning("hata: %s", e)
            errors += 1

        batch_size = 50
        if len(vectors) >= batch_size:
            write_vectors_to_redis(vectors)
            logger.info("    → %s vector Redis'e yazildi", len(vectors))
            vectors = []

        if delay is not None and i < total:
            time.sleep(delay)

    if vectors:
        write_vectors_to_redis(vectors)
        logger.info("  → %s vector Redis'e yazildi", len(vectors))

    written = total - errors
    logger.info("Tamamlandi. %s/%s sirket basariyla vektorize edildi.", written, total)
    if errors:
        logger.info("  Hatalar: %s", errors)


# ----------------------------------------------------------------------
# Eski mum verisi temizligi
# ----------------------------------------------------------------------
RETENTION = {
    "1m": timedelta(days=7),
    "5m": timedelta(days=7),
    "15m": timedelta(days=30),
    "30m": timedelta(days=30),
    "1h": timedelta(days=90),
    "1d": timedelta(days=365 * 5),
}


def run_cleanup_old_data() -> None:
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
            logger.info("  %s: %s satir silindi (before %s)", interval, deleted, cutoff.date())
            total_deleted += deleted

    if total_deleted:
        logger.info("Toplam %s eski satir temizlendi.", total_deleted)
    else:
        logger.info("Temizlenecek veri yok.")


# ----------------------------------------------------------------------
# Redis fiyat cache on-isitma
# ----------------------------------------------------------------------
def _warm_one(ticker: str) -> None:
    get_price_history(ticker, "5y", "1d", hot=True)


def run_warm_price_cache() -> None:
    from src.services.bist import get_bist_tickers_as_json_from_redis

    tickers = json.loads(get_bist_tickers_as_json_from_redis())
    logger.info("Warming %s tickers with 10 parallel workers...", len(tickers))

    start_time = time.time()
    done = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for raw in tickers:
            ticker = raw if raw.endswith(".IS") else f"{raw}.IS"
            future = executor.submit(_warm_one, ticker)
            futures[future] = ticker

        for future in as_completed(futures):
            ticker = futures[future]
            done += 1
            try:
                future.result()
                logger.info("[%s/%s] OK  %s", done, len(tickers), ticker)
            except Exception as e:
                errors += 1
                logger.warning("[%s/%s] ERR %s: %s", done, len(tickers), ticker, e)

    elapsed = time.time() - start_time
    logger.info("Done. %s tickers warmed in %ss (%s min), %s errors", len(tickers), int(elapsed), round(elapsed / 60, 1), errors)
