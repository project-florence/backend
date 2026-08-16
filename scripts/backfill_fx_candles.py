"""Backfill years of daily FX & precious-metals candles into rate_candles.

Operational script — **run manually, NOT a crontab job** (design spec Faz 3:
"rate_candles backfill: scripts/backfill_fx_candles.py"). Pulls 1d OHLCV
history from yfinance through the finance providers (USDTRY=X / EURTRY=X /
GBPTRY=X via ``yfinance_fx``; GC=F / SI=F / PL=F / PA=F via
``yfinance_metals``) and upserts the batch into ``rate_candles`` via
``storage.persist_candles`` (idempotent upsert — safe to re-run). The shared
1.5 s yfinance rate limiter (``clients/yfinance.py`` pattern, implemented in
``src/finance/providers/base.py``) is applied inside the providers, and every
yfinance call stays in ``asyncio.to_thread``.

Usage (run manually):
    python scripts/backfill_fx_candles.py                 # 5 years, all yfinance symbols
    python scripts/backfill_fx_candles.py --years 2       # shorter window
    python scripts/backfill_fx_candles.py --start 2021-01-01 --end 2026-01-01
    python scripts/backfill_fx_candles.py --symbols USD,EUR,XAU-ONS
    python scripts/backfill_fx_candles.py --dry-run       # fetch + report, no DB writes
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# Repo convention (scripts/doctor.py pattern): make ``src`` importable when
# the script runs directly (sys.path[0] is scripts/ otherwise).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.config import init_config
from src.finance import storage
from src.finance.models import Candle, ProviderName
from src.finance.providers.registry import PROVIDERS, provider
from src.finance.symbols import SYMBOL_REGISTRY

logger = logging.getLogger(__name__)

BACKFILL_INTERVAL = "1d"


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def _parse_symbols(text: str | None) -> set[str] | None:
    if not text:
        return None
    requested = {s.strip() for s in text.split(",") if s.strip()}
    unknown = requested - set(SYMBOL_REGISTRY)
    if unknown:
        raise SystemExit(
            f"Unknown canonical symbols: {sorted(unknown)} — use registry names "
            f"(e.g. USD, EUR, XAU-ONS)."
        )
    return requested


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill yfinance daily FX/metal candles into rate_candles (run manually)."
    )
    parser.add_argument("--years", type=int, default=5,
                        help="Years of history to fetch (default: 5)")
    parser.add_argument("--start", default=None,
                        help="Explicit start date ISO (YYYY-MM-DD); overrides --years")
    parser.add_argument("--end", default=None,
                        help="Explicit end date ISO (YYYY-MM-DD); default: today")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated canonical symbols to backfill "
                             "(default: every symbol a yfinance provider serves)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and report only — write nothing to DB/Redis")
    args = parser.parse_args()

    init_config()

    end = _parse_date(args.end) or datetime.now(timezone.utc)
    start = _parse_date(args.start) or (end - timedelta(days=args.years * 365))
    if start >= end:
        parser.error("--start must be before --end")

    wanted = _parse_symbols(args.symbols)
    total = 0
    per_symbol: dict[str, int] = {}
    all_candles: list[Candle] = []

    for name in (ProviderName.YFINANCE_FX, ProviderName.YFINANCE_METALS):
        p = provider(name)
        if p is None:
            continue
        for symbol in sorted(p.provides):
            if wanted is not None and symbol not in wanted:
                continue
            try:
                candles = await p.fetch_candles(
                    symbol, interval=BACKFILL_INTERVAL, start=start, end=end
                )
            except Exception as exc:
                logger.warning("%s/%s fetch failed: %s", name.value, symbol, exc)
                continue
            if candles:
                all_candles.extend(candles)
                per_symbol[symbol] = len(candles)
                total += len(candles)
                logger.info("%s: %d candles (%s, %s -> %s)",
                            symbol, len(candles), name.value,
                            candles[0].ts.date(), candles[-1].ts.date())

    if not all_candles:
        logger.warning("No candles fetched (%s providers) — nothing to persist", len(PROVIDERS))
        return

    if args.dry_run:
        print(f"[dry-run] {total} candles for {len(per_symbol)} symbols would be upserted "
              f"into rate_candles (interval={BACKFILL_INTERVAL})")
        return

    ok = await storage.persist_candles(all_candles)
    if not ok:
        logger.error("DB persist failed — candles were NOT written to rate_candles")
        return

    # Warm per-symbol Redis cache (design spec 5.2: fx:candles:{symbol}:1d, 7 days).
    by_symbol: dict[str, list[Candle]] = {}
    for c in all_candles:
        by_symbol.setdefault(c.symbol, []).append(c)
    for symbol, candles in by_symbol.items():
        await storage.set_candles_cache(symbol, BACKFILL_INTERVAL, candles)

    print(f"Backfill complete: {total} candles for {len(per_symbol)} symbols "
          f"upserted into rate_candles (run manually; safe to re-run).")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())