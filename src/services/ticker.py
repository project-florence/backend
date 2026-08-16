from datetime import datetime, timedelta, timezone

from src.finance.models import AssetClass
from src.finance.symbols import SYMBOL_REGISTRY
from src.services.bist import get_bist_tickers_as_dict_from_redis
from src.services.economy import get_gold_prices, get_silver_price, get_gram_platinum_price, get_gram_palladium_price, get_currency, get_economy_rate_history


# Metal legacy keys come from the canonical registry (design spec 4.3) —
# no duplicated symbol list. "altin" is a historical alias with no canonical
# mapping; it is kept only for backward compatibility with ticker validation.
PRECIOUS_METAL_KEYS: list[str] = sorted({
    d.legacy_name
    for d in SYMBOL_REGISTRY.values()
    if d.asset_class is AssetClass.METAL and d.legacy_name
} | {"altin"})


async def is_valid_ticker(ticker: str) -> bool:
    if not ticker:
        return False

    if ticker.lower() in PRECIOUS_METAL_KEYS:
        return True

    ticker_upper = ticker.upper()
    if ticker_upper in await get_bist_tickers_as_dict_from_redis():
        return True

    currency_data = await get_currency()
    if isinstance(currency_data, dict) and "error" not in currency_data:
        if ticker_upper in currency_data:
            return True

    return False


async def get_all_valid_keys() -> list[str]:
    bist_tickers = list(await get_bist_tickers_as_dict_from_redis())
    currency_data = await get_currency()
    forex_keys = (
        [k for k in currency_data if k != "error"]
        if isinstance(currency_data, dict)
        else []
    )
    return bist_tickers + PRECIOUS_METAL_KEYS + forex_keys


async def get_price_history(ticker: str, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    if not ticker:
        return []

    now = datetime.now(timezone.utc)
    if end is None:
        end = now
    if start is None:
        start = end - timedelta(days=365)

    ticker_lower = ticker.lower()

    if ticker_lower in PRECIOUS_METAL_KEYS:
        return await get_economy_rate_history(ticker_lower, start, end)

    ticker_upper = ticker.upper()
    currency_data = await get_currency()
    if isinstance(currency_data, dict) and "error" not in currency_data:
        if ticker_upper in currency_data:
            return await get_economy_rate_history(ticker_upper, start, end)

    from src.services.price import get_price_history as get_stock_history
    days = (end - start).days
    if days <= 1:
        period = "1d"
    elif days <= 5:
        period = "5d"
    elif days <= 30:
        period = "1mo"
    elif days <= 90:
        period = "3mo"
    elif days <= 180:
        period = "6mo"
    elif days <= 365:
        period = "1y"
    elif days <= 730:
        period = "2y"
    else:
        period = "5y"

    candles = await get_stock_history(ticker_upper, period=period, interval="1d")
    return [c for c in candles if start <= datetime.fromisoformat(c["ts"]) <= end]


def _extract_price(raw) -> float | None:
    """Legacy quote entry or numeric value -> float.

    Values are already floats since Faz 4 (the economy bridge never emits
    display strings); this only tolerates the rare plain-string/decimal-comma
    leftovers from the legacy Redis fallback during the transition.
    """
    if isinstance(raw, dict):
        val = raw.get("Buying")
        if val is None:
            val = raw.get("Selling")
        raw = val
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None


async def get_current_price(ticker: str, interval: str = "5m") -> float | None:
    if not ticker:
        return None

    ticker_lower = ticker.lower()

    if ticker_lower in PRECIOUS_METAL_KEYS:
        gold_prices = await get_gold_prices()
        if isinstance(gold_prices, dict) and ticker_lower in gold_prices:
            return _extract_price(gold_prices[ticker_lower])
        if ticker_lower == "gumus":
            silver = await get_silver_price()
            if isinstance(silver, dict):
                return _extract_price(silver.get("gumus"))
        if ticker_lower == "gram-platin":
            plat = await get_gram_platinum_price()
            if isinstance(plat, dict):
                return _extract_price(plat.get("gram-platin"))
        if ticker_lower == "gram-paladyum":
            pal = await get_gram_palladium_price()
            if isinstance(pal, dict):
                return _extract_price(pal.get("gram-paladyum"))
        return None

    ticker_upper = ticker.upper()

    currency_data = await get_currency()
    if isinstance(currency_data, dict) and "error" not in currency_data:
        if ticker_upper in currency_data:
            return _extract_price(currency_data[ticker_upper])

    from src.services.price import get_current_price as get_stock_price
    return await get_stock_price(ticker_upper, interval=interval)
