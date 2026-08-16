"""Periyodik islerin cagrilabilir fonksiyonlari.

Bu fonksiyonlar hem `scripts/` altindaki CLI wrapper'larindan hem de
`src/cron/register.py` uzerinden in-process cron tarafindan calistirilir.
Hepsi async'dir; yfinance gibi sync agirlikli isler `asyncio.to_thread`
ile event loop'u bloklamadan calistirilir.
"""

import asyncio
import importlib
import json
import logging
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from src.analysis.stock_vector import company_vector, write_vectors_to_redis
from src.core.database import db, price_write_lock
from src.core.redis import r
from src.services.bist import get_bist_companies_as_dict_from_redis
from src.services.company import get_company_info
from src.services.price import INTRADAY_INTERVALS, get_price_history, invalidate_price_cache
from src.services.stats import get_all_stats
from src.utils.mapping import load_bist_mapping

from psycopg.sql import Identifier, SQL

from src.core.config import get_config
from src.finance import finance_service, storage
from src.finance.models import Candle, ProviderName
from src.finance.providers.registry import provider

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
INFO_TICKER_DELAY = 2

# Delisted (borsadan cikmis) ticker'larin tutuldugu Redis set'i. Her turda
# yf.download / get_company_info'yu 30-60s yavaslatan olu ticker'lar ilk
# basarisizlikta isaretlenir; sonraki turlarda tamamen atlanir.
DELISTED_KEY = "delisted_tickers"

# _refresh_company_info turu 20-94 dk surebildigi icin lock TTL'i 7200s:
# tur hala surerken lock expire olup ust uste binmesin.
INFO_LOCK_TTL = 7200


async def _load_delisted() -> set[str]:
    """Redis'teki delisted ticker setini dondurur (Redis down ise bos)."""
    try:
        members = await r.smembers(DELISTED_KEY)
        return set(members) if members else set()
    except Exception:
        return set()


async def _mark_delisted(ticker: str) -> None:
    """Basarisiz (buyuk olasilikla delisted) ticker'i Redis set'ine ekler."""
    try:
        await r.sadd(DELISTED_KEY, ticker)
    except Exception:
        pass

# Tier config: (name, cron_frequency, default_interval, batch_size)
TIERS = {
    "bist30": ("BIST30", timedelta(minutes=10), "5m", 15),
    "popular": ("POPÜLER", timedelta(hours=1), "30m", 20),
    "rest": ("DİĞER", timedelta(hours=12), "1d", 50),
}


# ----------------------------------------------------------------------
# Fiyat guncelleme
# ----------------------------------------------------------------------
async def _acquire_cron_lock(tier_name: str, ttl: int = 600) -> bool:
    lock_key = f"lock:cron:{tier_name}"
    acquired = await r.set(lock_key, "1", ex=ttl, nx=True)
    return acquired is not None


async def _release_cron_lock(tier_name: str) -> None:
    await r.delete(f"lock:cron:{tier_name}")


async def _ticker_sets() -> dict[str, list[str]]:
    mapping = load_bist_mapping()
    popular_tickers = list(mapping.keys())
    companies = await get_bist_companies_as_dict_from_redis()
    all_tickers = {c["ticker"] for c in companies}

    bist30_set = set(BIST30_TICKERS)
    popular_set = set(popular_tickers) & all_tickers
    rest_set = all_tickers - bist30_set - popular_set

    return {
        "bist30": list(bist30_set & all_tickers),
        "popular": list(popular_set),
        "rest": list(rest_set),
    }


async def _needs_update(ticker_list: list[str], now: datetime, max_age: timedelta, interval: str = "1d") -> list[str]:
    if not ticker_list:
        return []

    placeholders = ",".join(["%s"] * len(ticker_list))
    tickers_is = [t + ".IS" for t in ticker_list]

    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            f"""SELECT ticker, MAX(ts) as last_ts
                FROM price_candles
                WHERE ticker IN ({placeholders}) AND interval = %s
                GROUP BY ticker""",
            tickers_is + [interval],
        )
        rows = await cur.fetchall()
        last_ts_map = {r[0]: r[1] for r in rows}
    await db.release_current()

    need = []
    for t, t_is in zip(ticker_list, tickers_is):
        last_ts = last_ts_map.get(t_is)
        if last_ts is None or (now - last_ts) > max_age:
            need.append(t)

    return need


async def _download_prices(batch_tickers: list[str], period: str, interval: str):
    """yfinance batch indirmesini thread'de calistirir (event loop'u bloklamaz)."""
    def _do():
        with open("/dev/null", "w") as devnull:
            with redirect_stderr(devnull), redirect_stdout(devnull):
                return yf.download(batch_tickers, period=period, interval=interval, group_by="ticker", progress=False)
    return await asyncio.to_thread(_do)


async def _update_batch(batch_tickers: list[str], interval: str, period: str, tier_name: str, offset: int, total: int) -> None:
    logger.info("  %s [%s-%s/%s] %s indiriliyor...", tier_name, offset + 1, offset + len(batch_tickers), total, interval)

    try:
        df = await _download_prices(batch_tickers, period, interval)
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
    updated_tickers = []

    async with price_write_lock:
        try:
            async with db.cursor(row_factory=None) as cur:
                for ticker in batch_tickers:
                    if ticker not in available:
                        continue
                    try:
                        tdf = df[ticker] if multi else df
                        tdf = tdf.dropna(how="all")
                        if tdf.empty:
                            continue

                        values = []
                        for ts, row in tdf.iterrows():
                            ts_dt = ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts
                            values.append((
                                ticker, interval, ts_dt,
                                float(row["Open"]) if pd.notna(row["Open"]) else None,
                                float(row["High"]) if pd.notna(row["High"]) else None,
                                float(row["Low"]) if pd.notna(row["Low"]) else None,
                                float(row["Close"]) if pd.notna(row["Close"]) else None,
                                int(row["Volume"]) if pd.notna(row["Volume"]) else 0,
                            ))
                        if values:
                            await cur.executemany(
                                """INSERT INTO price_candles (ticker, interval, ts, open, high, low, close, volume)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                   ON CONFLICT (ticker, interval, ts)
                                   DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high,
                                                 low = EXCLUDED.low, close = EXCLUDED.close,
                                                 volume = EXCLUDED.volume""",
                                values,
                            )
                            count += len(values)
                        updated_tickers.append(ticker)
                    except Exception:
                        await db.rollback()
                        logger.warning("Batch '{}' icin yazma hatasi, kalan tickerlar atlaniyor".format(ticker))
                        break
                # Commit cursor blok icinde: blok cikisinda otomatik iade
                # rollback yapmasin (bekleyen yazilar korunur).
                await db.commit()
        except Exception:
            await db.rollback()
            logger.warning("Batch commit basarisiz, {} ticker yazilamadi".format(len(batch_tickers)))
            count = 0
            updated_tickers = []

    for ticker in updated_tickers:
        await invalidate_price_cache(ticker, interval)

    logger.info("%s mum kaydedildi, %s cache invalidated", count, len(updated_tickers))


async def _refresh_company_info(tier_keys: list[str]) -> None:
    logger.info("[INFO] Company info tazeleniyor...")

    if "popular" not in tier_keys:
        logger.info("[INFO] Popular kademesi istenmemis, atlaniyor.")
        return

    mapping = load_bist_mapping()
    popular_tickers = list(mapping.keys())

    # Delisted ticker'lari atla (her biri get_company_info'yu 30-60s uzatir).
    delisted = await _load_delisted()
    if delisted:
        popular_tickers = [t for t in popular_tickers if t not in delisted]

    total = len(popular_tickers)
    for i, ticker in enumerate(popular_tickers, 1):
        logger.info("  [%s/%s] %s...", i, total, ticker)
        try:
            profile = await get_company_info(ticker, use_cache=False)
            if profile:
                logger.info("OK (%s alan)", len(profile))
            else:
                # Surekli "veri yok" donen ticker: buyuk olasilikla delisted.
                # Sonraki turlarda yine 30-60s harcamamak icin isaretle.
                logger.info("veri yok — delisted olarak isaretleniyor")
                await _mark_delisted(ticker)
        except Exception as e:
            logger.warning("hata: %s — delisted olarak isaretleniyor", e)
            await _mark_delisted(ticker)

        if i < total:
            await asyncio.sleep(INFO_TICKER_DELAY)

    logger.info("[INFO] Company info tazeleme tamamlandi.")


async def update_tier(tier_name: str, interval: str | None = None, info: bool = False, no_lock: bool = False) -> int:
    if tier_name not in TIERS:
        raise ValueError(f"Bilinmeyen tier: {tier_name}")

    name, freq, default_interval, batch_size = TIERS[tier_name]
    effective_interval = interval or default_interval
    ticker_list = (await _ticker_sets())[tier_name]

    # Delisted ticker'lari fiyat turlarindan da cikar (yf.download batch'ini
    # 30-60s yavaslatirlar).
    delisted = await _load_delisted()
    if delisted:
        ticker_list = [t for t in ticker_list if t not in delisted]

    # Info turu (20-94 dk) dahil tum tur boyunca lock tutulur; TTL tur
    # suresini karsilasin ki lock expire olup ust uste binmesin.
    lock_ttl = INFO_LOCK_TTL if info else 600
    if not no_lock and not await _acquire_cron_lock(tier_name, ttl=lock_ttl):
        logger.info("[%s] %s — lock alinamadi (baskasi calisiyor), atlaniyor", name, effective_interval)
        return 0

    total_updated = 0
    try:
        now = datetime.now(timezone.utc)
        need_update = await _needs_update(ticker_list, now, freq, effective_interval)
        if not need_update:
            logger.info("[%s] %s — guncelleme gerektiren yok (%s ticker)", name, effective_interval, len(ticker_list))
        else:
            logger.info("[%s] %s — %s/%s ticker guncellenecek...", name, effective_interval, len(need_update), len(ticker_list))
            tickers_is = [t + ".IS" for t in need_update]
            period = "5d"
            for i in range(0, len(tickers_is), batch_size):
                batch = tickers_is[i: i + batch_size]
                await _update_batch(batch, effective_interval, period, name, i, len(tickers_is))
                total_updated += len(batch)

                if i + batch_size < len(tickers_is):
                    logger.info("  %ss bekleniyor...", BATCH_DELAY)
                    await asyncio.sleep(BATCH_DELAY)

        # Info tazeleme de lock kapsaminda: price_popular bu uzun turla
        # ust uste binmesin.
        if info and tier_name == "popular":
            await _refresh_company_info([tier_name])
    finally:
        await _release_cron_lock(tier_name)

    logger.info("Toplam %s ticker guncellendi.", total_updated)
    return total_updated


async def run_update_bist30() -> None:
    await update_tier("bist30")


async def run_update_popular() -> None:
    await update_tier("popular", info=True)


async def run_update_rest() -> None:
    await update_tier("rest")


async def run_update_daily_closes() -> None:
    """Tum hisselerin gunluk (1d) kapanis mumlarini tazeler."""
    companies = await get_bist_companies_as_dict_from_redis()
    all_tickers = sorted({c["ticker"] for c in companies})

    # Delisted ticker'lari atla (yf.download'u yavaslatirlar).
    delisted = await _load_delisted()
    if delisted:
        all_tickers = [t for t in all_tickers if t not in delisted]

    total = len(all_tickers)
    logger.info("Gunluk kapanis mumlari guncelleniyor: %s ticker", total)

    for i in range(0, total, 50):
        batch = [f"{t}.IS" for t in all_tickers[i:i + 50]]
        await _update_batch(batch, "1d", "5d", "DAILY-CLOSE", i, total)
        if i + 50 < total:
            logger.info("  %ss bekleniyor...", BATCH_DELAY)
            await asyncio.sleep(BATCH_DELAY)

    logger.info("Gunluk kapanis guncellemesi tamamlandi.")


# ----------------------------------------------------------------------
# Kredi dolumu
# ----------------------------------------------------------------------
async def run_credit_refill() -> None:
    from src.services.credits import daily_refill

    count = await daily_refill()
    logger.info("Free credits yenilendi. Etkilenen kullanici: %s", count)


# ----------------------------------------------------------------------
# Stock vector hesaplama
# ----------------------------------------------------------------------
async def run_seed_vectors(count: int = 200, delay: float | None = None) -> None:
    stats = await get_all_stats()
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
            profile = await get_company_info(ticker, use_cache=True)
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
            await write_vectors_to_redis(vectors)
            logger.info("    → %s vector Redis'e yazildi", len(vectors))
            vectors = []

        if delay is not None and i < total:
            await asyncio.sleep(delay)

    if vectors:
        await write_vectors_to_redis(vectors)
        logger.info("  → %s vector Redis'e yazildi", len(vectors))

    written = total - errors
    logger.info("Tamamlandi. %s/%s sirket basariyla vektorize edildi.", written, total)
    if errors:
        logger.info("  Hatalar: %s", errors)


# ----------------------------------------------------------------------
# Redis fiyat cache on-isitma
# ----------------------------------------------------------------------
async def _warm_one(ticker: str) -> None:
    await get_price_history(ticker, "5y", "1d", hot=True)


async def run_warm_price_cache() -> None:
    from src.services.bist import get_bist_tickers_as_json_from_redis

    tickers = json.loads(await get_bist_tickers_as_json_from_redis())
    logger.info("Warming %s tickers with 3 parallel workers...", len(tickers))

    start_time = time.time()
    done = 0
    errors = 0

    sem = asyncio.Semaphore(3)

    async def _warm(ticker: str) -> None:
        nonlocal done, errors
        try:
            async with sem:
                await _warm_one(ticker)
            done += 1
            logger.info("[%s/%s] OK  %s", done, len(tickers), ticker)
        except Exception as e:
            done += 1
            errors += 1
            logger.warning("[%s/%s] ERR %s: %s", done, len(tickers), ticker, e)

    tasks = []
    for raw in tickers:
        ticker = raw if raw.endswith(".IS") else f"{raw}.IS"
        tasks.append(asyncio.create_task(_warm(ticker)))
    await asyncio.gather(*tasks)

    elapsed = time.time() - start_time
    logger.info("Done. %s tickers warmed in %ss (%s min), %s errors", len(tickers), int(elapsed), round(elapsed / 60, 1), errors)


# ----------------------------------------------------------------------
# Finance pipeline cron tasks (Faz 2)
# Design: ANALYSIS/ekonomi-refactor-plani.md sections 6.1-6.3, 5.2.
# Redis NX locks reuse the existing _acquire_cron_lock/_release_cron_lock
# helpers (key pattern lock:cron:*); TTLs follow the design 6.1 table.
# ----------------------------------------------------------------------

# Lock TTLs (seconds) — design spec 6.1 table.
ECONOMY_REFRESH_LOCK_TTL = 600        # matches finance.refresh_interval_s default
FX_CANDLES_LOCK_TTL = 7200            # long round: 7 symbols, ~1.5 s limiter each
RATE_ANALYSIS_LOCK_TTL = 1800
RETENTION_CLEANUP_LOCK_TTL = 3600

# Retention policy (design spec 5.3): (table, date column, retention period).
# Column names verified against src/core/database.py init_db() DDL:
#   market_rates.timestamp, economy_rates.ts, rate_candles.ts, rate_metrics.computed_at
RETENTION_RULES: tuple[tuple[str, str, str], ...] = (
    ("market_rates", "timestamp", "90 days"),
    ("economy_rates", "ts", "2 years"),
    ("rate_candles", "ts", "5 years"),
    ("rate_metrics", "computed_at", "1 year"),
)

_FX_CANDLES_INTERVAL = "1d"
_FX_CANDLES_WINDOW_DAYS = 5


async def run_refresh_economy() -> None:
    """Refresh the FX/metals quote pipeline (design spec 6.1-6.3, 5.2).

    Every 10 minutes, 7/24. Redis NX lock ``lock:cron:economy_refresh`` with a
    TTL of ``finance.refresh_interval_s`` (default 600 s) prevents overlapping
    rounds; ``FinanceService.refresh_all()`` runs the provider fan-out, fallback
    chains, gram derivation, persist and ``fx:quotes`` cache write. Fully
    failure-tolerant: provider/DB/Redis errors are logged and the round ends
    instead of crashing (design spec 6.3).
    """
    try:
        ttl = int((get_config() or {})["finance"].get("refresh_interval_s", ECONOMY_REFRESH_LOCK_TTL))
    except Exception:
        ttl = ECONOMY_REFRESH_LOCK_TTL
    if not await _acquire_cron_lock("economy_refresh", ttl=ttl):
        logger.info("[ECONOMY-REFRESH] lock not acquired (another round running), skipping")
        return
    try:
        bundle = await finance_service.refresh_all()
        logger.info(
            "[ECONOMY-REFRESH] %d quotes (source=%s, remaining=%s)",
            len(bundle.quotes), bundle.source, bundle.remaining,
        )
    except Exception:
        logger.exception("[ECONOMY-REFRESH] refresh round failed, skipping")
    finally:
        await _release_cron_lock("economy_refresh")
        # Cron rule: hand any held connection back to the pool.
        await db.release_current()


async def run_fx_candles_daily() -> None:
    """Upsert the latest 1d FX/metal candles into rate_candles (18:35 TRT).

    Fetches daily candles for every canonical symbol the yfinance providers
    serve (USD/EUR/GBP via yfinance_fx, XAU-ONS/XAG-ONS/XPT-ONS/XPD-ONS via
    yfinance_metals) through ``BaseProvider.fetch_candles`` — yfinance stays
    inside ``asyncio.to_thread`` behind the shared 1.5 s rate limiter. Results
    are persisted via ``storage.persist_candles`` (idempotent upsert) and the
    Redis ``fx:candles:*`` warm cache is rewritten (design spec 6.1 / 5.2).
    Lock ``lock:cron:fx_candles`` TTL 7200 s.
    """
    if not await _acquire_cron_lock("fx_candles", ttl=FX_CANDLES_LOCK_TTL):
        logger.info("[FX-CANDLES] lock not acquired (another round running), skipping")
        return
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=_FX_CANDLES_WINDOW_DAYS)
        candles: list[Candle] = []
        for name in (ProviderName.YFINANCE_FX, ProviderName.YFINANCE_METALS):
            p = provider(name)
            if p is None:
                continue
            for symbol in sorted(p.provides):
                try:
                    fetched = await p.fetch_candles(
                        symbol, interval=_FX_CANDLES_INTERVAL, start=start, end=end
                    )
                except Exception as exc:  # provider contract: never raises; defensive
                    logger.warning("[FX-CANDLES] %s/%s fetch failed: %s", name.value, symbol, exc)
                    continue
                if fetched:
                    candles.extend(fetched)
                    logger.info("[FX-CANDLES] %s: %d candles (%s)", symbol, len(fetched), name.value)

        if not candles:
            logger.warning("[FX-CANDLES] no candles fetched this round")
            return

        if not await storage.persist_candles(candles):
            # Cache-first layer (design 6.3): DB failure must not kill the round.
            logger.warning("[FX-CANDLES] DB persist failed, serving cache only")

        # Warm Redis per-symbol cache (design spec 5.2: fx:candles:{symbol}:1d, 7 days).
        by_symbol: dict[str, list[Candle]] = {}
        for c in candles:
            by_symbol.setdefault(c.symbol, []).append(c)
        for symbol, sym_candles in by_symbol.items():
            await storage.set_candles_cache(symbol, _FX_CANDLES_INTERVAL, sym_candles)
        logger.info("[FX-CANDLES] %d candles persisted for %d symbols", len(candles), len(by_symbol))
    except Exception:
        logger.exception("[FX-CANDLES] unexpected round failure, skipping")
    finally:
        await _release_cron_lock("fx_candles")
        await db.release_current()


async def run_rate_analysis_daily() -> None:
    """Pre-compute analysis metrics for all canonical symbols (Faz 3 stub).

    Full-signature placeholder (design spec 6.1-6.2): acquires
    ``lock:cron:rate_analysis`` (TTL 1800 s), then gracefully returns while the
    ``src.finance.analysis`` module does not exist yet. Faz 3 fills the body:
    rate_candles + economy_rates -> AnalysisResult per symbol -> Redis
    ``fx:analysis:*`` + ``rate_metrics`` persist.
    """
    if not await _acquire_cron_lock("rate_analysis", ttl=RATE_ANALYSIS_LOCK_TTL):
        logger.info("[RATE-ANALYSIS] lock not acquired (another round running), skipping")
        return
    try:
        try:
            importlib.import_module("src.finance.analysis")
        except ImportError:
            logger.info("[RATE-ANALYSIS] analysis module not ready yet (Faz 3), skipping")
            return
        # Faz 3: iterate SYMBOL_REGISTRY, compute AnalysisResult per symbol,
        # storage.persist_metrics + storage.set_analysis_cache.
        logger.info("[RATE-ANALYSIS] analysis module present, Faz 3 implementation pending")
    finally:
        await _release_cron_lock("rate_analysis")
        await db.release_current()


async def run_retention_cleanup() -> None:
    """Apply the retention DELETE policy (design spec 5.3).

    Weekly, Sunday 03:00 TRT. Each table is purged independently using column
    names from init_db() DDL (RETENTION_RULES): market_rates.timestamp,
    economy_rates.ts, rate_candles.ts, rate_metrics.computed_at. A missing
    table or any DB error logs and skips that table — never raises (6.3).
    Lock ``lock:cron:retention_cleanup`` TTL 3600 s.
    """
    if not await _acquire_cron_lock("retention_cleanup", ttl=RETENTION_CLEANUP_LOCK_TTL):
        logger.info("[RETENTION] lock not acquired (another round running), skipping")
        return
    try:
        for table, column, period in RETENTION_RULES:
            try:
                # Table/column names come from our own constant tuple; composed
                # via psycopg.sql so the period stays a bound parameter.
                statement = SQL("DELETE FROM {} WHERE {} < NOW() - INTERVAL %s").format(
                    Identifier(table), Identifier(column)
                )
                async with db.cursor(row_factory=None) as cur:
                    await cur.execute(statement, (period,))
                    deleted = cur.rowcount
                    await db.commit()
                logger.info("[RETENTION] %s: %s rows purged (older than %s)", table, deleted, period)
            except Exception as exc:
                await db.rollback()
                message = str(exc)
                if "does not exist" in message.lower():
                    logger.warning("[RETENTION] table %s not present, skipping", table)
                else:
                    logger.warning("[RETENTION] purge of %s failed: %s", table, message)
    finally:
        await _release_cron_lock("retention_cleanup")
        await db.release_current()
