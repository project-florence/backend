"""Persistence + Redis key management for the finance pipeline.

Single door for ``rate_candles`` / ``rate_metrics`` / ``rate_provider_status``
(PostgreSQL) and the ``fx:*`` Redis namespace (design spec 5.1 / 5.2). Every
method is failure-tolerant: it logs and returns a neutral value, never raises
— the refresh flow must keep running when the DB or Redis hiccups.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from src.core.database import db
from src.core.redis import r
from src.finance.models import (
    AnalysisResult,
    AssetClass,
    Candle,
    ProviderName,
    ProviderStatus,
    Quote,
    QuoteBundle,
)
from src.finance.symbols import SYMBOL_REGISTRY

logger = logging.getLogger(__name__)

MARKET_TIMEZONE = ZoneInfo("Europe/Istanbul")

# Redis keys and TTLs (design spec 5.2).
REDIS_QUOTES = "fx:quotes"
REDIS_QUOTES_STALE = "fx:quotes:stale"
REDIS_ANALYSIS_PREFIX = "fx:analysis:"
REDIS_CANDLES_PREFIX = "fx:candles:"
REDIS_PROVIDER_PREFIX = "fx:provider:"
REDIS_REFRESH_LOCK = "fx:lock:refresh"

TTL_QUOTES_STALE = 60
TTL_ANALYSIS = 86400
TTL_CANDLES = 604800
TTL_PROVIDER = 86400
TTL_REFRESH_LOCK = 90

# Own market_rates snapshot type (legacy types "gold"/"currency"/... untouched).
MARKET_RATES_DATA_TYPE = "finance_quotes"

# economy_rates JSONB price keys kept compatible with legacy consumers
# (portfolio charts read "Buying"); values are floats — never display strings.
_LEGACY_TYPE = {
    AssetClass.FX: "Currency",
    AssetClass.METAL: "Metal",
    AssetClass.COMMODITY: "Commodity",
}


def today_start_utc() -> datetime:
    """Start of the current Istanbul day, expressed in UTC."""
    now = datetime.now(timezone.utc)
    today_local = now.astimezone(MARKET_TIMEZONE).date()
    return datetime.combine(today_local, datetime.min.time(), tzinfo=MARKET_TIMEZONE).astimezone(
        timezone.utc
    )


# ---------------------------------------------------------------------------
# Redis helpers (fx:* namespace)
# ---------------------------------------------------------------------------


async def get_quotes_cache() -> QuoteBundle | None:
    raw = await r.get(REDIS_QUOTES)
    if not raw:
        return None
    try:
        return QuoteBundle.model_validate_json(raw)
    except Exception:
        return None


async def set_quotes_cache(bundle: QuoteBundle, ttl: int) -> None:
    try:
        await r.set(REDIS_QUOTES, bundle.model_dump_json(), ex=ttl)
    except Exception:
        pass


async def set_stale_marker(error_msg: str) -> None:
    """Signals a failed collection round (short TTL) — design spec 5.2."""
    try:
        payload = json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "status": "error", "last_error": error_msg},
            ensure_ascii=False,
        )
        await r.set(REDIS_QUOTES_STALE, payload, ex=TTL_QUOTES_STALE)
    except Exception:
        pass


async def get_analysis_cache(symbol: str) -> AnalysisResult | None:
    raw = await r.get(f"{REDIS_ANALYSIS_PREFIX}{symbol}")
    if not raw:
        return None
    try:
        return AnalysisResult.model_validate_json(raw)
    except Exception:
        return None


async def set_analysis_cache(symbol: str, result: AnalysisResult) -> None:
    try:
        await r.set(f"{REDIS_ANALYSIS_PREFIX}{symbol}", result.model_dump_json(), ex=TTL_ANALYSIS)
    except Exception:
        pass


async def get_candles_cache(symbol: str, interval: str) -> list[Candle] | None:
    raw = await r.get(f"{REDIS_CANDLES_PREFIX}{symbol}:{interval}")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return [Candle.model_validate(item) for item in payload]
    except Exception:
        return None


async def set_candles_cache(symbol: str, interval: str, candles: list[Candle]) -> None:
    try:
        payload = json.dumps([c.model_dump(mode="json") for c in candles], ensure_ascii=False)
        await r.set(f"{REDIS_CANDLES_PREFIX}{symbol}:{interval}", payload, ex=TTL_CANDLES)
    except Exception:
        pass


async def get_provider_status_cache(name: ProviderName) -> ProviderStatus | None:
    raw = await r.get(f"{REDIS_PROVIDER_PREFIX}{name.value}")
    if not raw:
        return None
    try:
        return ProviderStatus.model_validate_json(raw)
    except Exception:
        return None


async def set_provider_status_cache(status: ProviderStatus) -> None:
    try:
        await r.set(
            f"{REDIS_PROVIDER_PREFIX}{status.provider.value}",
            status.model_dump_json(),
            ex=TTL_PROVIDER,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PostgreSQL persistence
# ---------------------------------------------------------------------------


async def persist_quotes(bundle: QuoteBundle) -> bool:
    """One snapshot row in market_rates + per-symbol rows in economy_rates.

    economy_rates keeps its legacy PK (ticker, ts) and ON CONFLICT DO NOTHING;
    the new ``source`` column records which provider won each symbol.
    """
    if not bundle.quotes:
        return False
    first_src = next(iter(bundle.quotes.values())).source.value
    try:
        now = bundle.ts
        snapshot = {
            "ts": now.isoformat(),
            "quotes": {sym: q.model_dump(mode="json") for sym, q in bundle.quotes.items()},
        }
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "INSERT INTO market_rates (data_type, data, source, meta) VALUES (%s, %s, %s, %s)",
                (
                    MARKET_RATES_DATA_TYPE,
                    json.dumps(snapshot, ensure_ascii=False),
                    first_src,
                    json.dumps(
                        {"remaining": bundle.remaining, "provider": first_src, "tz": "UTC"},
                        ensure_ascii=False,
                    ),
                ),
            )
            rows = []
            for symbol, q in bundle.quotes.items():
                d = SYMBOL_REGISTRY.get(symbol)
                price = {
                    "Buying": q.buying,
                    "Selling": q.selling,
                    "Change": q.change_pct,
                    "Type": _LEGACY_TYPE.get(d.asset_class, "Asset") if d else "Asset",
                    "currency": q.currency,
                    "unit": q.unit,
                    "stale": q.stale,
                }
                rows.append(
                    (
                        symbol,
                        now,
                        json.dumps(price, ensure_ascii=False),
                        q.source.value,
                    )
                )
            await cur.executemany(
                "INSERT INTO economy_rates (ticker, ts, price, source) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                rows,
            )
            # Commit inside the block so the auto-release does not roll back DDL/data.
            await db.commit()
        return True
    except Exception:
        await db.rollback()
        logger.warning("persist_quotes failed (%d symbols)", len(bundle.quotes), exc_info=True)
        return False


async def persist_candles(candles: list[Candle]) -> bool:
    """Idempotent upsert of FX/metal candles into rate_candles."""
    if not candles:
        return False
    try:
        rows = [
            (
                c.symbol,
                c.interval,
                c.ts,
                c.open,
                c.high,
                c.low,
                c.close,
                c.volume,
                c.source.value if c.source else None,
            )
            for c in candles
        ]
        async with db.cursor(row_factory=None) as cur:
            await cur.executemany(
                "INSERT INTO rate_candles (symbol, interval, ts, open, high, low, close, volume, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (symbol, interval, ts) DO UPDATE SET "
                "open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, "
                "close = EXCLUDED.close, volume = EXCLUDED.volume, source = EXCLUDED.source",
                rows,
            )
            await db.commit()
        return True
    except Exception:
        await db.rollback()
        logger.warning("persist_candles failed (%d candles)", len(candles), exc_info=True)
        return False


async def persist_metrics(result: AnalysisResult) -> bool:
    """Daily pre-computed analysis summary into rate_metrics (upsert per day)."""
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "INSERT INTO rate_metrics (symbol, computed_at, analysis) VALUES (%s, %s, %s) "
                "ON CONFLICT (symbol, computed_at) DO UPDATE SET analysis = EXCLUDED.analysis",
                (result.symbol, result.computed_at, result.model_dump_json()),
            )
            await db.commit()
        return True
    except Exception:
        await db.rollback()
        logger.warning("persist_metrics failed (%s)", result.symbol, exc_info=True)
        return False


async def persist_provider_status(status: ProviderStatus) -> bool:
    """Upsert provider health row (circuit breaker persistence) + Redis mirror."""
    ok = False
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "INSERT INTO rate_provider_status "
                "(provider, last_success, last_error, consecutive_failures, circuit_open, last_error_msg) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (provider) DO UPDATE SET "
                "last_success = EXCLUDED.last_success, last_error = EXCLUDED.last_error, "
                "consecutive_failures = EXCLUDED.consecutive_failures, "
                "circuit_open = EXCLUDED.circuit_open, last_error_msg = EXCLUDED.last_error_msg",
                (
                    status.provider.value,
                    status.last_success,
                    status.last_error,
                    status.consecutive_failures,
                    status.circuit_open,
                    status.last_error_msg,
                ),
            )
            await db.commit()
        ok = True
    except Exception:
        await db.rollback()
        logger.warning("persist_provider_status failed (%s)", status.provider.value, exc_info=True)
    await set_provider_status_cache(status)
    return ok


# ---------------------------------------------------------------------------
# PostgreSQL reads
# ---------------------------------------------------------------------------


async def load_previous_closes(symbols: list[str], before_ts: datetime) -> dict[str, float]:
    """Last 1d close per symbol before ``before_ts`` (for change_pct)."""
    if not symbols:
        return {}
    try:
        async with db.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT ON (symbol) symbol, close FROM rate_candles "
                "WHERE interval = '1d' AND symbol = ANY(%s) AND ts < %s "
                "ORDER BY symbol, ts DESC",
                (symbols, before_ts),
            )
            rows = await cur.fetchall()
        return {row["symbol"]: row["close"] for row in rows if row["close"] is not None}
    except Exception:
        logger.warning("load_previous_closes failed", exc_info=True)
        return {}


async def load_candles(
    symbol: str,
    interval: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Candle]:
    try:
        query = (
            "SELECT symbol, interval, ts, open, high, low, close, volume, source "
            "FROM rate_candles WHERE symbol = %s AND interval = %s"
        )
        params: list = [symbol, interval]
        if start is not None:
            query += " AND ts >= %s"
            params.append(start)
        if end is not None:
            query += " AND ts <= %s"
            params.append(end)
        query += " ORDER BY ts"
        async with db.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [
            Candle(
                symbol=row["symbol"],
                interval=row["interval"],
                ts=row["ts"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                source=ProviderName(row["source"]) if row["source"] else None,
            )
            for row in rows
        ]
    except Exception:
        logger.warning("load_candles failed (%s)", symbol, exc_info=True)
        return []


async def load_records_many(symbols: Iterable[str], *, year_days: int = 365) -> dict[str, dict]:
    """Record/extreme summary for many symbols in a constant number of queries.

    Replaces the old per-symbol ``load_records`` (3 queries × N symbols) with
    two batched queries across ``symbol = ANY(...)``: one conditional-aggregation
    pass for all-time + 52w extremes, one DISTINCT ON for each symbol's last close.
    """
    wanted = [s for s in symbols if s in SYMBOL_REGISTRY]
    if not wanted:
        return {}
    year_ago = datetime.now(timezone.utc) - timedelta(days=year_days)
    out: dict[str, dict] = {}
    for sym in wanted:
        out[sym] = {
            "symbol": sym,
            "all_time_high": None,
            "all_time_low": None,
            "high_52w": None,
            "low_52w": None,
            "rank_in_52w": None,
            "last_close": None,
        }
    try:
        async with db.cursor() as cur:
            # 1) all-time + 52w extremes in a single grouped pass.
            await cur.execute(
                "SELECT symbol, "
                "  MAX(high) AS at_high, MIN(low) AS at_low, "
                "  MAX(high) FILTER (WHERE ts >= %s) AS w_high, "
                "  MIN(low)  FILTER (WHERE ts >= %s) AS w_low "
                "FROM rate_candles "
                "WHERE interval = '1d' AND symbol = ANY(%s) "
                "GROUP BY symbol",
                (year_ago, year_ago, wanted),
            )
            agg_rows = await cur.fetchall()
            # 2) last 1d close per symbol.
            await cur.execute(
                "SELECT DISTINCT ON (symbol) symbol, close FROM rate_candles "
                "WHERE interval = '1d' AND symbol = ANY(%s) "
                "ORDER BY symbol, ts DESC",
                (wanted,),
            )
            last_rows = await cur.fetchall()
        for row in agg_rows:
            rec = out.get(row["symbol"])
            if rec is None:
                continue
            rec["all_time_high"], rec["all_time_low"] = row["at_high"], row["at_low"]
            rec["high_52w"], rec["low_52w"] = row["w_high"], row["w_low"]
        for row in last_rows:
            rec = out.get(row["symbol"])
            if rec is not None:
                rec["last_close"] = row["close"]
        for rec in out.values():
            high52, low52, close = rec["high_52w"], rec["low_52w"], rec["last_close"]
            if high52 is not None and low52 is not None and close is not None and high52 > low52:
                rec["rank_in_52w"] = (close - low52) / (high52 - low52)
    except Exception:
        logger.warning("load_records_many failed (%d symbols)", len(wanted), exc_info=True)
    return out


async def load_records(symbol: str) -> dict:
    """Record/extreme summary for one symbol (all-time + 52w aggregates)."""
    return (await load_records_many([symbol])).get(symbol, {
        "symbol": symbol,
        "all_time_high": None,
        "all_time_low": None,
        "high_52w": None,
        "low_52w": None,
        "rank_in_52w": None,
        "last_close": None,
    })


async def load_latest_db_snapshot(symbols: set[str] | None = None) -> dict[str, Quote]:
    """Last known-good snapshot: own market_rates row, then per-symbol economy_rates.

    Served as the final fallback with ``stale=True`` and source DB_SNAPSHOT.
    """
    quotes: dict[str, Quote] = {}
    # 1) our finance_quotes snapshot (one row, full bundle)
    try:
        async with db.cursor() as cur:
            await cur.execute(
                "SELECT data FROM market_rates WHERE data_type = %s ORDER BY id DESC LIMIT 1",
                (MARKET_RATES_DATA_TYPE,),
            )
            row = await cur.fetchone()
    except Exception:
        row = None
    if row:
        data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        if isinstance(data, dict):
            snapshot_ts = None
            raw_ts = data.get("ts")
            if isinstance(raw_ts, str):
                try:
                    snapshot_ts = datetime.fromisoformat(raw_ts)
                except ValueError:
                    pass
            for sym, qd in (data.get("quotes") or {}).items():
                if symbols is not None and sym not in symbols:
                    continue
                if sym not in SYMBOL_REGISTRY or not isinstance(qd, dict):
                    continue
                try:
                    quote = Quote(**qd)
                    quote.stale = True
                    quote.source = ProviderName.DB_SNAPSHOT
                    if quote.ts is None and snapshot_ts is not None:
                        quote.ts = snapshot_ts
                    quotes[sym] = quote
                except Exception:
                    continue
        if quotes:
            return quotes
    # 2) per-symbol economy_rates fallback (canonical tickers only)
    wanted = set(symbols) if symbols is not None else set(SYMBOL_REGISTRY)
    for sym in sorted(wanted):
        if sym not in SYMBOL_REGISTRY:
            continue
        try:
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT ts, price, source FROM economy_rates "
                    "WHERE ticker = %s ORDER BY ts DESC LIMIT 1",
                    (sym,),
                )
                row = await cur.fetchone()
        except Exception:
            row = None
        if not row:
            continue
        price = json.loads(row["price"]) if isinstance(row["price"], str) else row["price"]
        if not isinstance(price, dict) or price.get("Buying") is None:
            continue
        d = SYMBOL_REGISTRY[sym]
        buying = price.get("Buying")
        quotes[sym] = Quote(
            symbol=sym,
            buying=buying,
            selling=price.get("Selling"),
            price=buying if d.quote_kind.value == "price" else price.get("price"),
            change_pct=price.get("Change"),
            currency=d.currency,
            unit=d.unit,
            source=ProviderName.DB_SNAPSHOT,
            ts=row["ts"],
            stale=True,
        )
    return quotes


async def load_provider_statuses() -> dict[str, ProviderStatus]:
    """All persisted provider health rows keyed by provider name."""
    try:
        async with db.cursor() as cur:
            await cur.execute(
                "SELECT provider, last_success, last_error, consecutive_failures, "
                "circuit_open, last_error_msg FROM rate_provider_status"
            )
            rows = await cur.fetchall()
        out: dict[str, ProviderStatus] = {}
        for row in rows:
            try:
                out[row["provider"]] = ProviderStatus(
                    provider=ProviderName(row["provider"]),
                    last_success=row["last_success"],
                    last_error=row["last_error"],
                    consecutive_failures=row["consecutive_failures"] or 0,
                    circuit_open=bool(row["circuit_open"]),
                    last_error_msg=row["last_error_msg"],
                )
            except ValueError:
                continue
        return out
    except Exception:
        logger.warning("load_provider_statuses failed", exc_info=True)
        return {}