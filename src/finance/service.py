"""FinanceService — orchestration of the FX & precious metals pipeline.

Design spec 3.5 / 6.3. Read path is cache-first (Redis ``fx:quotes``, TTL =
``finance.refresh_interval_s``) with single-flight refresh; the collection
path pulls providers in parallel (Semaphore(4)), walks the fallback chain per
symbol, applies gram derivation, computes ``change_pct`` from our own
rate_candles close series, persists, and rewrites the cache. When everything
fails the last resort is the DB snapshot served with ``stale=True``.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from src.core.config import get_config
from src.core.database import db
from src.core.redis import r
from src.finance import storage
from src.finance.models import (
    AnalysisResult,
    Candle,
    ProviderName,
    ProviderStatus,
    Quote,
    QuoteBundle,
)
from src.finance.providers.registry import PROVIDERS, chains_for, provider
from src.finance.symbols import SYMBOL_REGISTRY

logger = logging.getLogger(__name__)

_DERIVATION_RULE = "derived: ons x usd / gram_per_oz"


class FinanceService:
    """Orchestrator: cache-first quotes, single-flight refresh, fallback chains."""

    def __init__(self) -> None:
        self._fetch_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public read path
    # ------------------------------------------------------------------

    async def get_quotes(self, symbols: list[str] | None = None) -> QuoteBundle:
        """Cache-first quote read. Order (design spec 3.5):
        1) Redis fx:quotes hit -> filtered bundle
        2) single-flight refresh (double-check under lock)
        3) last resort: DB snapshot with stale=True
        """
        wanted = self._normalize(symbols)
        cached = await self._cached_bundle(wanted)
        if cached is not None:
            return cached
        async with self._fetch_lock:
            cached = await self._cached_bundle(wanted)
            if cached is not None:
                return cached
            held = await self._try_acquire_refresh_lock()
            try:
                bundle = await self.refresh_all()
            finally:
                if held:
                    await self._release_refresh_lock()
        if bundle.quotes:
            return self._filter_bundle(bundle, wanted)
        return await self._stale_fallback(wanted)

    async def refresh_symbols(self, symbols: set[str], *, write_cache: bool = False) -> QuoteBundle:
        """Partial refresh (single-symbol / gap backfill / startup warm-up).

        Persists the requested subset to the DB and updates provider health, but
        by default does NOT overwrite the full Redis ``fx:quotes`` bundle — a
        partial collection must never clobber the cross-symbol cache.
        """
        wanted = {s for s in symbols if s in SYMBOL_REGISTRY}
        return await self._collect(wanted, write_cache=write_cache)

    async def refresh_all(self) -> QuoteBundle:
        """Full collection over every canonical symbol (cron + cache miss)."""
        return await self._collect(set(SYMBOL_REGISTRY), write_cache=True)

    async def get_candles(
        self,
        symbol: str,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        if symbol not in SYMBOL_REGISTRY:
            return []
        # Cache-first (fixes write-only Redis cache): the cache holds the full
        # series per (symbol, interval) — written daily by cron and populated
        # here on miss — so a hit is filtered by the requested window in memory
        # and the DB is only touched when the cache is cold.
        cached = await storage.get_candles_cache(symbol, interval)
        if cached is not None:
            return self._filter_candles(cached, start, end)
        # On miss, load the full series (window-agnostic) and populate the cache
        # so later window requests are served without hitting the DB.
        full = await storage.load_candles(symbol, interval)
        if full:
            await storage.set_candles_cache(symbol, interval, full)
        return self._filter_candles(full, start, end)
        # yfinance backfill for gaps lands with Faz 3 (run_fx_candles_daily).

    @staticmethod
    def _filter_candles(
        candles: list[Candle],
        start: datetime | None,
        end: datetime | None,
    ) -> list[Candle]:
        if start is None and end is None:
            return candles
        return [
            c
            for c in candles
            if (start is None or c.ts >= start) and (end is None or c.ts <= end)
        ]

    async def get_analysis(self, symbol: str) -> AnalysisResult | None:
        """Pre-computed daily metrics (Faz 3 cron computes+stores; read-only here)."""
        if symbol not in SYMBOL_REGISTRY:
            return None
        return await storage.get_analysis_cache(symbol)

    async def get_records(self, symbol: str) -> dict:
        if symbol not in SYMBOL_REGISTRY:
            return {}
        return await storage.load_records(symbol)

    async def get_records_many(self, symbols: Iterable[str]) -> dict[str, dict]:
        """Batched record summary over many symbols (single constant set of queries)."""
        wanted = {s for s in symbols if s in SYMBOL_REGISTRY}
        return await storage.load_records_many(wanted)

    async def get_provider_status(self) -> list[ProviderStatus]:
        """Live circuit state merged with persisted rows (restart recovery)."""
        cfg = get_config()["finance"]
        persisted = await storage.load_provider_statuses()
        out: list[ProviderStatus] = []
        now = datetime.now(timezone.utc)
        for name, p in PROVIDERS.items():
            live = p.status()
            db_status = persisted.get(name.value)
            if db_status is not None:
                if live.last_success is None:
                    live.last_success = db_status.last_success
                if live.last_error is None:
                    live.last_error = db_status.last_error
                if live.last_error_msg is None:
                    live.last_error_msg = db_status.last_error_msg
                # Restart recovery: honor a persisted open circuit whose
                # cooldown window has not yet elapsed (no in-memory state).
                if (
                    live.consecutive_failures == 0
                    and db_status.consecutive_failures > 0
                    and db_status.last_error is not None
                ):
                    cooldown_left = db_status.last_error + timedelta(
                        seconds=float(cfg["circuit_open_s"])
                    )
                    if cooldown_left > now:
                        live.consecutive_failures = db_status.consecutive_failures
                        live.circuit_open = True
            out.append(live)
        return sorted(out, key=lambda s: s.provider.value)

    async def restore_circuit_breakers(self) -> None:
        """Rehydrate provider in-memory circuits from persisted health (startup).

        Fixes "circuit not restored on restart": a process that restarts now
        honors a persisted open circuit whose cooldown has not elapsed, so it
        no longer blindly issues full provider tries. Falls back to the Redis
        mirror (``fx:provider:*``) when a provider has no DB row yet.
        """
        db_statuses = await storage.load_provider_statuses()
        for name, p in PROVIDERS.items():
            status = db_statuses.get(name.value)
            if status is None:
                status = await storage.get_provider_status_cache(name)
            if status is not None:
                p.restore_from_status(status)

    async def warm_startup(self) -> None:
        """One full refresh at startup (wired into main.py lifespan in Faz 2)."""
        await self.restore_circuit_breakers()
        logger.info("finance warm_startup: refreshing all quotes")
        try:
            bundle = await self.refresh_all()
            logger.info(
                "finance warm_startup done: %d quotes (source=%s, remaining=%s)",
                len(bundle.quotes), bundle.source, bundle.remaining,
            )
        except Exception:
            logger.exception("finance warm_startup failed")
        finally:
            await db.release_current()

    # ------------------------------------------------------------------
    # Collection core
    # ------------------------------------------------------------------

    async def _collect(self, symbols: set[str], *, write_cache: bool = True) -> QuoteBundle:
        started = datetime.now(timezone.utc)
        if not symbols:
            return QuoteBundle(ts=started, quotes={})

        # Assign symbols to every capable provider along their chains. All
        # providers are fetched in parallel (design spec 3.5); the merge below
        # picks the first chain winner per symbol, so a primary source that
        # silently omits a symbol (e.g. GenelPara missing JOD/CZK/ILS) still
        # gets covered by the next source in the chain.
        provider_symbols: dict[ProviderName, set[str]] = {}
        for symbol in symbols:
            for name in chains_for(symbol):
                if name is ProviderName.DB_SNAPSHOT:
                    continue
                p = provider(name)
                if p is not None and p.is_available and symbol in p.provides:
                    provider_symbols.setdefault(name, set()).add(symbol)

        # Fetch every involved provider in parallel (Semaphore(4)).
        semaphore = asyncio.Semaphore(4)
        results: dict[ProviderName, dict[str, Quote]] = {}

        async def _fetch(name: ProviderName, syms: set[str]) -> None:
            p = provider(name)
            if p is None:
                return
            try:
                async with semaphore:
                    results[name] = await p.fetch_quotes(syms)
            except Exception as exc:  # providers never raise; defensive only
                logger.warning("provider %s fetch crashed: %s", name.value, exc)
                results[name] = {}

        await asyncio.gather(
            *(_fetch(name, syms) for name, syms in provider_symbols.items())
        )

        # Merge: first provider on the chain that delivered a quote wins.
        quotes: dict[str, Quote] = {}
        for symbol in symbols:
            for name in chains_for(symbol):
                if name is ProviderName.DB_SNAPSHOT:
                    continue
                q = results.get(name, {}).get(symbol)
                if q is not None:
                    quotes[symbol] = q
                    break

        self._derive_grams(quotes)
        await self._attach_change_pct(quotes)

        genelpara = provider(ProviderName.GENELPARA)
        remaining = getattr(genelpara, "last_remaining", None) if genelpara else None
        bundle = QuoteBundle(
            ts=started,
            source=next(iter(quotes.values())).source if quotes else None,
            quotes=quotes,
            remaining=remaining,
        )

        # Persist + cache (DB failure must not kill the pipeline — cache wins).
        await storage.persist_quotes(bundle)
        for p in PROVIDERS.values():
            await storage.persist_provider_status(p.status())
        if quotes:
            if write_cache:
                await storage.set_quotes_cache(
                    bundle, int(get_config()["finance"]["refresh_interval_s"])
                )
        else:
            await storage.set_stale_marker("all providers failed this round")
            logger.error("finance refresh_all: no quotes produced (%s symbols)", len(symbols))
        logger.info(
            "finance refresh: %d/%d quotes in %.1fs (source=%s, remaining=%s)",
            len(quotes), len(symbols),
            (datetime.now(timezone.utc) - started).total_seconds(),
            bundle.source, remaining,
        )
        return bundle

    def _derive_grams(self, quotes: dict[str, Quote]) -> None:
        """Derivation rule: XAU-GRAM = XAU-ONS x USD / gram_per_oz.

        Runs only for symbols a direct source did not supply; the ounce/TRY
        legs must both be present in the same collection round.
        """
        try:
            gram_per_oz = float(get_config()["finance"]["gram_per_oz"])
        except Exception:
            gram_per_oz = 31.1035
        if gram_per_oz <= 0:
            return
        for symbol, d in SYMBOL_REGISTRY.items():
            if not d.derived_from or symbol in quotes:
                continue
            dep_ons, dep_fx = d.derived_from
            ons_q = quotes.get(dep_ons)
            fx_q = quotes.get(dep_fx)
            if ons_q is None or fx_q is None:
                continue
            ons_val = ons_q.price if ons_q.price is not None else ons_q.buying
            fx_val = (
                fx_q.buying
                if fx_q.buying is not None
                else (fx_q.price if fx_q.price is not None else fx_q.selling)
            )
            if ons_val is None or fx_val is None:
                continue
            value = ons_val * fx_val / gram_per_oz
            quotes[symbol] = Quote(
                symbol=symbol,
                buying=value,
                selling=value,
                price=value,
                currency=d.currency,
                unit=d.unit,
                source=ons_q.source,  # audit: winner of the ounce leg
                ts=datetime.now(timezone.utc),
                extra={"derived": True, "formula": f"{dep_ons} x {dep_fx} / {gram_per_oz}"},
            )

    async def _attach_change_pct(self, quotes: dict[str, Quote]) -> None:
        """Daily change from our own series (previous 1d close in rate_candles).

        Never uses the source's ``degisim`` field (weekend zeros) nor the
        legacy economy_rates window — plan 7.2 rule: 1d close = rate_candles.
        """
        if not quotes:
            return
        prev = await storage.load_previous_closes(list(quotes), storage.today_start_utc())
        for symbol, q in quotes.items():
            previous = prev.get(symbol)
            base = q.buying if q.buying is not None else q.price
            if previous and base and previous > 0:
                q.change_pct = (base - previous) / previous * 100

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(symbols: list[str] | None) -> set[str] | None:
        if symbols is None:
            return None
        return {s for s in symbols if s in SYMBOL_REGISTRY}

    @staticmethod
    def _filter_bundle(bundle: QuoteBundle, wanted: set[str] | None) -> QuoteBundle:
        if wanted is None:
            return bundle
        return QuoteBundle(
            ts=bundle.ts,
            source=bundle.source,
            quotes={s: bundle.quotes[s] for s in wanted if s in bundle.quotes},
            remaining=bundle.remaining,
        )

    async def _cached_bundle(self, wanted: set[str] | None) -> QuoteBundle | None:
        """Serve whatever of the requested symbols the last full refresh collected.

        A symbol no provider can deliver must not permanently defeat the cache:
        a hit is still useful as long as at least one requested symbol is present.
        """
        bundle = await storage.get_quotes_cache()
        if bundle is None or not bundle.quotes:
            return None
        if wanted is None:
            return bundle
        available = wanted & set(bundle.quotes)
        if not available:
            # Hiçbiri yoksa önbellek işe yaramaz; gerçek bir yenileme denensin.
            return None
        return self._filter_bundle(bundle, available)

    async def _stale_fallback(self, wanted: set[str] | None) -> QuoteBundle:
        """Last resort: last known-good DB snapshot, marked stale."""
        quotes = await storage.load_latest_db_snapshot(wanted)
        ts = (
            max((q.ts for q in quotes.values()), default=datetime.now(timezone.utc))
            if quotes
            else datetime.now(timezone.utc)
        )
        return QuoteBundle(ts=ts, source=ProviderName.DB_SNAPSHOT, quotes=quotes)

    @staticmethod
    async def _try_acquire_refresh_lock() -> bool:
        try:
            return (await r.set(storage.REDIS_REFRESH_LOCK, "1", ex=storage.TTL_REFRESH_LOCK, nx=True)) is not None
        except Exception:
            return False

    @staticmethod
    async def _release_refresh_lock() -> None:
        try:
            await r.delete(storage.REDIS_REFRESH_LOCK)
        except Exception:
            pass