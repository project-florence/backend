"""Unit tests for src/services/ticker.py.

Hermetic: all external data sources (BIST Redis cache, economy bridge, stock
price service) are monkeypatched with canned async functions. No Redis, DB or
network access happens.
"""

from datetime import datetime, timedelta, timezone

import pytest

import src.services.price as price_module
import src.services.ticker as ticker_module


# ---------------------------------------------------------------------------
# is_valid_ticker
# ---------------------------------------------------------------------------


async def test_is_valid_ticker_metal_short_circuits():
    assert await ticker_module.is_valid_ticker("gumus") is True
    assert await ticker_module.is_valid_ticker("GRAM-ALTIN") is True


async def test_is_valid_ticker_bist_and_currency(monkeypatch):
    async def _bist():
        return {"THYAO": {"name": "THY"}}

    async def _cur():
        return {"USD": {"Buying": 41.0}}

    monkeypatch.setattr(ticker_module, "get_bist_tickers_as_dict_from_redis", _bist)
    monkeypatch.setattr(ticker_module, "get_currency", _cur)

    assert await ticker_module.is_valid_ticker("THYAO") is True
    assert await ticker_module.is_valid_ticker("usd") is True
    assert await ticker_module.is_valid_ticker("ZZZZ") is False


async def test_is_valid_ticker_empty():
    assert await ticker_module.is_valid_ticker("") is False
    assert await ticker_module.is_valid_ticker(None) is False


# ---------------------------------------------------------------------------
# get_all_valid_keys
# ---------------------------------------------------------------------------


async def test_get_all_valid_keys(monkeypatch):
    async def _bist():
        return {"THYAO": 1, "GARAN": 1}

    async def _cur():
        return {"USD": {"Buying": 41.0}, "EUR": {"Buying": 45.0}, "error": None}

    monkeypatch.setattr(ticker_module, "get_bist_tickers_as_dict_from_redis", _bist)
    monkeypatch.setattr(ticker_module, "get_currency", _cur)

    keys = await ticker_module.get_all_valid_keys()
    assert "THYAO" in keys
    assert "USD" in keys and "EUR" in keys
    assert "error" not in keys
    assert "gumus" in keys


# ---------------------------------------------------------------------------
# _extract_price
# ---------------------------------------------------------------------------


def test_extract_price_variants():
    assert ticker_module._extract_price({"Buying": 40.5, "Selling": 40.8}) == 40.5
    assert ticker_module._extract_price({"Selling": 40.8}) == 40.8
    assert ticker_module._extract_price({"Buying": None}) is None
    assert ticker_module._extract_price(42.0) == 42.0
    assert ticker_module._extract_price("42,5") == 42.5
    assert ticker_module._extract_price(None) is None
    assert ticker_module._extract_price(True) is None
    assert ticker_module._extract_price("abc") is None


# ---------------------------------------------------------------------------
# get_price_history
# ---------------------------------------------------------------------------


async def test_get_price_history_empty_ticker():
    assert await ticker_module.get_price_history("") == []


async def test_get_price_history_metal(monkeypatch):
    async def _hist(ticker, start, end):
        assert ticker == "gumus"
        return [{"ts": "2026-08-01T00:00:00+00:00", "price": 100.0}]

    monkeypatch.setattr(ticker_module, "get_economy_rate_history", _hist)
    hist = await ticker_module.get_price_history("gumus")
    assert hist[0]["price"] == 100.0


async def test_get_price_history_currency(monkeypatch):
    async def _cur():
        return {"USD": {"Buying": 41.0}}

    async def _hist(ticker, start, end):
        assert ticker == "USD"
        return [{"ts": "2026-08-01T00:00:00+00:00", "price": 41.0}]

    monkeypatch.setattr(ticker_module, "get_currency", _cur)
    monkeypatch.setattr(ticker_module, "get_economy_rate_history", _hist)
    hist = await ticker_module.get_price_history("USD")
    assert hist[0]["price"] == 41.0


async def test_get_price_history_stock(monkeypatch):
    captured = {}

    async def _stock_hist(ticker, period, interval, hot=False):
        captured["ticker"] = ticker
        captured["period"] = period
        return [
            {"ts": "2026-08-01T00:00:00+00:00", "close": 100.0},
            {"ts": "2026-08-02T00:00:00+00:00", "close": 101.0},
        ]

    monkeypatch.setattr(price_module, "get_price_history", _stock_hist)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 2, tzinfo=timezone.utc)
    hist = await ticker_module.get_price_history("THYAO", start=start, end=end)
    assert captured["ticker"] == "THYAO"
    assert captured["period"] == "1d"
    assert len(hist) == 2


# ---------------------------------------------------------------------------
# get_current_price
# ---------------------------------------------------------------------------


async def test_get_current_price_empty():
    assert await ticker_module.get_current_price("") is None


async def test_get_current_price_metal_gold(monkeypatch):
    async def _gold():
        return {"gram-altin": {"Buying": 2500.0, "Selling": 2510.0}}

    monkeypatch.setattr(ticker_module, "get_gold_prices", _gold)
    assert await ticker_module.get_current_price("gram-altin") == 2500.0


async def test_get_current_price_metal_silver(monkeypatch):
    async def _gold():
        return {}

    async def _silver():
        return {"gumus": {"Buying": 30.0, "Selling": 30.2}}

    monkeypatch.setattr(ticker_module, "get_gold_prices", _gold)
    monkeypatch.setattr(ticker_module, "get_silver_price", _silver)
    assert await ticker_module.get_current_price("gumus") == 30.0


async def test_get_current_price_metal_platinum(monkeypatch):
    async def _gold():
        return {}

    async def _plat():
        return {"gram-platin": {"Buying": 900.0}}

    monkeypatch.setattr(ticker_module, "get_gold_prices", _gold)
    monkeypatch.setattr(ticker_module, "get_gram_platinum_price", _plat)
    assert await ticker_module.get_current_price("gram-platin") == 900.0


async def test_get_current_price_metal_palladium(monkeypatch):
    async def _gold():
        return {}

    async def _pal():
        return {"gram-paladyum": {"Buying": 700.0}}

    monkeypatch.setattr(ticker_module, "get_gold_prices", _gold)
    monkeypatch.setattr(ticker_module, "get_gram_palladium_price", _pal)
    assert await ticker_module.get_current_price("gram-paladyum") == 700.0


async def test_get_current_price_metal_unknown(monkeypatch):
    async def _gold():
        return {}

    async def _silver():
        return {}

    async def _plat():
        return {}

    async def _pal():
        return {}

    monkeypatch.setattr(ticker_module, "get_gold_prices", _gold)
    monkeypatch.setattr(ticker_module, "get_silver_price", _silver)
    monkeypatch.setattr(ticker_module, "get_gram_platinum_price", _plat)
    monkeypatch.setattr(ticker_module, "get_gram_palladium_price", _pal)
    assert await ticker_module.get_current_price("gram-altin") is None


async def test_get_current_price_currency(monkeypatch):
    async def _cur():
        return {"USD": {"Buying": 41.2, "Selling": 41.5}}

    monkeypatch.setattr(ticker_module, "get_currency", _cur)
    assert await ticker_module.get_current_price("USD") == 41.2


async def test_get_current_price_stock(monkeypatch):
    async def _cur():
        return {}

    async def _stock_price(ticker, interval="5m"):
        assert ticker == "THYAO"
        return 123.5

    monkeypatch.setattr(ticker_module, "get_currency", _cur)
    monkeypatch.setattr(price_module, "get_current_price", _stock_price)
    assert await ticker_module.get_current_price("THYAO") == 123.5