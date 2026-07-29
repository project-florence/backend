from datetime import datetime, timedelta, timezone

from services.bist import get_bist_tickers_as_dict_from_redis
from services.economy import get_gold_prices, get_silver_price, get_gram_platinum_price, get_gram_palladium_price, get_currency, get_economy_rate_history


PRECIOUS_METAL_KEYS = [
    "ons", "gram-altin", "gram-has-altin", "ceyrek-altin", "yarim-altin",
    "tam-altin", "cumhuriyet-altini", "ata-altin", "14-ayar-altin",
    "18-ayar-altin", "ikibucuk-altin", "altin", "gremse-altin",
    "22-ayar-bilezik", "besli-altin", "resat-altin", "hamit-altin",
    "gumus", "gram-platin", "gram-paladyum"
]


def is_valid_ticker(ticker: str) -> bool:
    if not ticker:
        return False

    if ticker.lower() in PRECIOUS_METAL_KEYS:
        return True

    ticker_upper = ticker.upper()
    if ticker_upper in get_bist_tickers_as_dict_from_redis():
        return True

    currency_data = get_currency()
    if isinstance(currency_data, dict) and "error" not in currency_data:
        if ticker_upper in currency_data:
            return True

    return False


def get_all_valid_keys() -> list[str]:
    bist_tickers = list(get_bist_tickers_as_dict_from_redis())
    currency_data = get_currency()
    forex_keys = (
        [k for k in currency_data if k != "error"]
        if isinstance(currency_data, dict)
        else []
    )
    return bist_tickers + PRECIOUS_METAL_KEYS + forex_keys


def get_price_history(ticker: str, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    if not ticker:
        return []

    now = datetime.now(timezone.utc)
    if end is None:
        end = now
    if start is None:
        start = end - timedelta(days=365)

    ticker_lower = ticker.lower()

    if ticker_lower in PRECIOUS_METAL_KEYS:
        return get_economy_rate_history(ticker_lower, start, end)

    ticker_upper = ticker.upper()
    currency_data = get_currency()
    if isinstance(currency_data, dict) and "error" not in currency_data:
        if ticker_upper in currency_data:
            return get_economy_rate_history(ticker_upper, start, end)

    from services.price import get_price_history as get_stock_history
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

    candles = get_stock_history(ticker_upper, period=period, interval="1d")
    return [c for c in candles if start <= datetime.fromisoformat(c["ts"]) <= end]


def get_current_price(ticker: str) -> float | None:
    if not ticker:
        return None

    ticker_lower = ticker.lower()

    if ticker_lower in PRECIOUS_METAL_KEYS:
        gold_prices = get_gold_prices()
        if isinstance(gold_prices, dict) and ticker_lower in gold_prices:
            return gold_prices[ticker_lower]
        if ticker_lower == "gumus":
            silver = get_silver_price()
            if isinstance(silver, dict):
                return silver.get("gumus")
        if ticker_lower == "gram-platin":
            plat = get_gram_platinum_price()
            if isinstance(plat, dict):
                return plat.get("gram-platin")
        if ticker_lower == "gram-paladyum":
            pal = get_gram_palladium_price()
            if isinstance(pal, dict):
                return pal.get("gram-paladyum")
        return None

    ticker_upper = ticker.upper()

    currency_data = get_currency()
    if isinstance(currency_data, dict) and "error" not in currency_data:
        if ticker_upper in currency_data:
            return currency_data[ticker_upper]

    from services.price import get_current_price as get_stock_price
    return get_stock_price(ticker_upper)
