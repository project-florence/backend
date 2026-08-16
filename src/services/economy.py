"""Legacy FX/precious-metals service — compatibility bridge to FinanceService.

Design spec (ANALYSIS/ekonomi-refactor-plani.md) section 8.1 / Faz 4: every
legacy function keeps its exact signature (the api layer, ``ticker.py``, the
report agent and ``portfolio.py`` all call them) but delegates to the
canonical ``FinanceService``. All payloads are numeric — ``Buying``/``Selling``
are floats and ``Change`` is ``change_pct`` as a float (never a comma display
string): the display-format layer (``_to_tr_string`` and friends) is gone, so
the frontend "0.00" bug class (plan 8.3) is closed at the source.

During the transition the legacy Redis keys (``gold_prices``,
``silver_price``, ``gram_platinum_price``, ``gram_palladium_price``,
``currency``) are only *read* as a fallback when the new pipeline produced
nothing, and only then for as long as their TTL lasts; nothing writes to them
anymore. Physical key cleanup is a deploy-time concern.
"""

import json
import logging
from datetime import datetime

from src.core.database import db
from src.core.redis import r
from src.finance import finance_service
from src.finance.models import AssetClass, Quote
from src.finance.symbols import SYMBOL_REGISTRY

logger = logging.getLogger(__name__)

# Canonical symbol -> legacy output key (reverse of the registry's
# ``legacy_name``). FX symbols are canonical and legacy at the same time;
# metals carry a transition-only legacy name. Built here from the registry —
# the single source of truth stays ``SYMBOL_REGISTRY``.
_CANONICAL_TO_LEGACY: dict[str, str] = {
    symbol: (d.legacy_name or symbol) for symbol, d in SYMBOL_REGISTRY.items()
}

# Reverse direction: legacy metal key -> canonical symbol (used by the
# history bridge to read both old and new economy_rates rows).
_LEGACY_TO_CANONICAL: dict[str, str] = {
    d.legacy_name: symbol
    for symbol, d in SYMBOL_REGISTRY.items()
    if d.legacy_name
}

# Legacy gold-prices set (the 16 symbols of the old GENELPARA_GOLD_MAP).
_GOLD_CANONICAL: tuple[str, ...] = (
    "XAU-ONS", "XAU-GRAM", "XAU-HAS", "XAU-CEYREK", "XAU-YARIM", "XAU-TAM",
    "XAU-CUMHURIYET", "XAU-ATA", "XAU-14-AYAR", "XAU-18-AYAR", "XAU-22-BILEZIK",
    "XAU-IKIBUCUK", "XAU-BESLI", "XAU-GREMSE", "XAU-RESAT", "XAU-HAMIT",
)

# Legacy Type labels are preserved exactly (portfolio/report consumers rely on
# them): gold varieties + silver were "Gold", platinum/palladium "Commodity",
# currencies "Currency".
_GOLD_TYPE = "Gold"
_COMMODITY_TYPE = "Commodity"
_CURRENCY_TYPE = "Currency"


def _legacy_value_to_float(value) -> float | None:
    """Tolerant numeric parse for legacy cache strings (transition only)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _legacy_entry(quote: Quote, type_label: str) -> dict:
    """Canonical Quote -> legacy ``{Buying, Selling, Change, Type}`` dict.

    All numbers are floats; ``Change`` is ``change_pct`` (``None`` when there
    is no previous close — callers must render "—", never 0.00). ``stale`` is
    set when the quote came from a DB snapshot fallback.
    """
    entry: dict = {
        "Buying": quote.buying,
        "Selling": quote.selling,
        "Change": quote.change_pct,
        "Type": type_label,
        "change_pct": quote.change_pct,  # explicit numeric alias (plan 8.3)
        "currency": quote.currency,
        "unit": quote.unit,
        "source": quote.source.value if quote.source else None,
        "ts": quote.ts.isoformat() if quote.ts else None,
    }
    if quote.change_pct is not None:
        # Optional display hint — the authoritative field is always numeric.
        entry["change_text"] = f"%{quote.change_pct:+.2f}"
    if quote.stale:
        entry["stale"] = True
    return entry


async def _legacy_redis_fallback(key: str) -> dict | None:
    """Transition fallback: old Redis bucket, normalized to floats.

    Only used while rolling deployments still write the legacy keys (their
    TTL expires them shortly). Values are converted from the old comma-string
    format so downstream never sees display strings.
    """
    try:
        raw = await r.get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not data:
        return None
    out: dict[str, dict] = {}
    for legacy_key, item in data.items():
        if not isinstance(item, dict):
            continue
        change = _legacy_value_to_float(item.get("Change"))
        entry: dict = {
            "Buying": _legacy_value_to_float(item.get("Buying")),
            "Selling": _legacy_value_to_float(item.get("Selling")),
            "Change": change,
            "Type": item.get("Type"),
            "stale": True,  # served from the legacy bucket during transition
        }
        if change is not None:
            entry["change_pct"] = change
        out[legacy_key] = entry
    return out


async def _legacy_quotes(
    canonical: list[str],
    type_label: str,
    legacy_redis_key: str | None = None,
) -> dict[str, dict]:
    """New pipeline first; legacy Redis key only as a transition fallback."""
    bundle = await finance_service.get_quotes(canonical)
    out: dict[str, dict] = {}
    for symbol in canonical:
        quote = bundle.quotes.get(symbol)
        if quote is None:
            continue
        out[_CANONICAL_TO_LEGACY[symbol]] = _legacy_entry(quote, type_label)
    if out:
        return out
    if legacy_redis_key is not None:
        fallback = await _legacy_redis_fallback(legacy_redis_key)
        if fallback:
            return fallback
    return {}


async def get_gold_prices() -> dict:
    """Legacy gold prices (16 varieties), keyed by legacy names — all floats."""
    return await _legacy_quotes(list(_GOLD_CANONICAL), _GOLD_TYPE, "gold_prices")


async def get_silver_price() -> dict:
    """Legacy silver price (``{"gumus": {...}}``) — floats."""
    return await _legacy_quotes(["XAG-GRAM"], _GOLD_TYPE, "silver_price")


async def get_gram_platinum_price() -> dict:
    """Legacy gram platinum price (``{"gram-platin": {...}}``) — floats."""
    return await _legacy_quotes(["XPT-GRAM"], _COMMODITY_TYPE, "gram_platinum_price")


async def get_gram_palladium_price() -> dict:
    """Legacy gram palladium price (``{"gram-paladyum": {...}}``) — floats."""
    return await _legacy_quotes(["XPD-GRAM"], _COMMODITY_TYPE, "gram_palladium_price")


async def get_currency() -> dict:
    """Legacy currency map (ISO codes quoted in TRY) — floats.

    The canonical FX set (``SYMBOL_REGISTRY``) covers 48 codes; exotic codes
    outside that set are no longer served (design spec 4.2 canonical set).
    """
    codes = sorted(
        symbol for symbol, d in SYMBOL_REGISTRY.items()
        if d.asset_class is AssetClass.FX
    )
    return await _legacy_quotes(codes, _CURRENCY_TYPE, "currency")


async def get_economy_rate_history(ticker: str, start: datetime, end: datetime) -> list[dict]:
    """Legacy ``economy_rates`` time series for a ticker.

    Reads rows under the canonical symbol (new snapshots are persisted with
    canonical tickers) and, when ``ticker`` is a legacy metal name, the legacy
    ticker rows too (old snapshots during the transition) — deduplicated by
    timestamp with canonical rows winning. The legacy response shape
    ``[{"ts", "price"}]`` is preserved for portfolio charts.
    """
    canonical = _LEGACY_TO_CANONICAL.get(ticker)
    query_tickers = [canonical] if canonical else [ticker]
    if canonical and canonical != ticker:
        query_tickers.append(ticker)
    try:
        async with db.cursor() as cur:
            await cur.execute(
                "SELECT ticker, ts, price FROM economy_rates "
                "WHERE ticker = ANY(%s) AND ts >= %s AND ts <= %s "
                "ORDER BY ts, CASE ticker WHEN %s THEN 0 ELSE 1 END",
                (query_tickers, start, end, canonical or ticker),
            )
            rows = await cur.fetchall()
    except Exception:
        logger.warning("get_economy_rate_history failed (%s)", ticker, exc_info=True)
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        ts = row["ts"].isoformat()
        if ts in seen:
            continue
        seen.add(ts)
        out.append({"ts": ts, "price": row["price"]})
    return out