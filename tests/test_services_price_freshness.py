"""Unit tests for src/services/price.py -- gunluk kapanis tazelik telafisi.

Tamamen hermetik: DB ``fake_db``, kilit ``fake_redis`` ile karsilanir; ag
(yfinance) ``_fetch_and_store`` monkeypatch'i ile kesilir. Beklenen seans
tarihi gercek ``expected_last_session_date()``'ten turetilir, boylece test
takvim gunune bagli kalmaz.
"""

from datetime import timedelta

import src.services.price as price_module
from src.services.market import expected_last_session_date, session_ts


def _behind_expected():
    """Beklenen son seanstan kesinlikle eski bir kanonik ts dondurur."""
    expected = expected_last_session_date()
    return session_ts(expected - timedelta(days=2))


# ---------------------------------------------------------------------------
# ensure_recent_daily_candle
# ---------------------------------------------------------------------------


async def test_ensure_returns_false_when_current(fake_db, fake_redis, monkeypatch):
    expected = expected_last_session_date()
    fake_db.queue_fetchone((session_ts(expected),))
    called = []

    async def fake_fetch(ticker, interval, start, end):
        called.append(ticker)

    monkeypatch.setattr(price_module, "_fetch_and_store", fake_fetch)

    assert await price_module.ensure_recent_daily_candle("asels") is False
    assert called == []


async def test_ensure_fills_when_behind(fake_db, fake_redis, monkeypatch):
    fake_db.queue_fetchone((_behind_expected(),))
    calls = []

    async def fake_fetch(ticker, interval, start, end):
        calls.append((ticker, interval, start, end))

    monkeypatch.setattr(price_module, "_fetch_and_store", fake_fetch)

    assert await price_module.ensure_recent_daily_candle("asels") is True
    assert len(calls) == 1
    ticker, interval, _start, _end = calls[0]
    assert ticker == "ASELS.IS"
    assert interval == "1d"
    # Basarili turda kilit serbest birakildi.
    assert await fake_redis.get("refresh_lock:ASELS.IS:1d") is None


async def test_ensure_uses_30_day_window_without_existing_candle(fake_db, fake_redis, monkeypatch):
    fake_db.queue_fetchone(None)
    calls = []

    async def fake_fetch(ticker, interval, start, end):
        calls.append((start, end))

    monkeypatch.setattr(price_module, "_fetch_and_store", fake_fetch)

    assert await price_module.ensure_recent_daily_candle("ASELS") is True
    start, end = calls[0]
    assert end - start >= timedelta(days=29)


async def test_ensure_skips_on_lock_contention(fake_db, fake_redis, monkeypatch):
    fake_db.queue_fetchone((_behind_expected(),))
    await fake_redis.set("refresh_lock:ASELS.IS:1d", "1", nx=True)
    called = []

    async def fake_fetch(*args, **kwargs):
        called.append(args)

    monkeypatch.setattr(price_module, "_fetch_and_store", fake_fetch)

    assert await price_module.ensure_recent_daily_candle("ASELS") is False
    assert called == []
    # Kilit baskasinin: telafi onu silmemeli.
    assert await fake_redis.get("refresh_lock:ASELS.IS:1d") == "1"


async def test_ensure_swallows_upstream_error(fake_db, fake_redis, monkeypatch):
    fake_db.queue_fetchone((_behind_expected(),))

    async def boom(*args, **kwargs):
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(price_module, "_fetch_and_store", boom)

    assert await price_module.ensure_recent_daily_candle("ASELS") is False
    # Hata olsa da kilit finally ile birakildi.
    assert await fake_redis.get("refresh_lock:ASELS.IS:1d") is None


# ---------------------------------------------------------------------------
# get_current_price: 1d dali kapali piyasada telafi tetikler
# ---------------------------------------------------------------------------


async def test_get_current_price_closed_branch_triggers_fill(fake_db, monkeypatch):
    fake_db.queue_fetchone({"close": 100.0, "ts": _behind_expected()}, {"close": 101.0})
    monkeypatch.setattr(price_module, "get_market_status", lambda: "closed")
    calls = []

    async def fake_ensure(ticker):
        calls.append(ticker)
        return True

    monkeypatch.setattr(price_module, "ensure_recent_daily_candle", fake_ensure)

    price = await price_module.get_current_price("ASELS", "1d")

    assert price == 101.0
    assert calls == ["ASELS.IS"]


async def test_get_current_price_returns_stale_close_when_fill_fails(fake_db, monkeypatch):
    fake_db.queue_fetchone({"close": 100.0, "ts": _behind_expected()})
    monkeypatch.setattr(price_module, "get_market_status", lambda: "closed")

    async def fake_ensure(ticker):
        return False

    monkeypatch.setattr(price_module, "ensure_recent_daily_candle", fake_ensure)

    assert await price_module.get_current_price("ASELS", "1d") == 100.0
