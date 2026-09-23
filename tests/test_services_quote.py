"""Unit tests for src/services/quote.py -- BIST quote montaji.

Tamamen hermetik: mum satirlari ``fake_db``, profil onbellegi ``fake_redis``
uzerinden verilir; ``get_market_status`` monkeypatch ile sabitlenir ve
``quote.datetime.now`` dondurulur ki testler takvim gunune bagli olmasin.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

import src.services.quote as quote_module
from src.services.market import MARKET_TIMEZONE, session_ts

TICKER = "ASELS.IS"


def _ist(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=MARKET_TIMEZONE)


def _freeze_now(monkeypatch, moment_ist):
    """``quote`` icindeki ``datetime.now``'u sabit bir ana dondurur."""
    fixed = moment_ist.astimezone(timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(quote_module, "datetime", _FrozenDatetime)


def _daily(d, close):
    return (TICKER, "1d", session_ts(d), close)


@pytest.fixture(autouse=True)
def _stub_price_ensure(monkeypatch):
    """``get_quotes`` gunluk telafi adimini hermetik tutar.

    Varsayilan olarak hicbir ticker icin telafi yapmaz (``False``) -- boylece
    mevcut testler gercek yfinance agina cikmaz. Tazelik davranisini test
    eden testler bunu kendi monkeypatch'i ile ezer.
    """
    from src.services import price as price_module

    async def _noop(ticker):
        return False

    monkeypatch.setattr(price_module, "ensure_recent_daily_candle", _noop)


# ---------------------------------------------------------------------------
# (1) Bugunun mumu yok: bayat isaretlenmeli, degisim yuzdesi uydurulmamali.
# ---------------------------------------------------------------------------


async def test_missing_today_candle_is_stale_and_change_is_sane(fake_db, fake_redis, monkeypatch):
    _freeze_now(monkeypatch, _ist(2026, 9, 23, 19, 0))  # kapanistan sonra
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "closed")
    fake_db.queue_fetchall([
        _daily(date(2026, 9, 22), 100.0),
        _daily(date(2026, 9, 21), 99.0),
    ])

    quote = (await quote_module.get_quotes(["ASELS"]))["ASELS"]

    assert quote["is_stale"] is True
    assert quote["market_status"] == "closed"
    # 09-23 seansi DB'de yok; 09-22/09-21 farkindan makul bir yuzde beklenir.
    assert quote["change_pct"] == pytest.approx((100.0 - 99.0) / 99.0 * 100, abs=1e-4)
    assert abs(quote["change_pct"]) < 10


# ---------------------------------------------------------------------------
# (2) Ayni seans icin iki yazim konvansiyonu: kanonik ts tercih edilmeli.
# ---------------------------------------------------------------------------


async def test_dual_convention_duplicate_rows_prefer_canonical_ts(fake_db, fake_redis, monkeypatch):
    _freeze_now(monkeypatch, _ist(2026, 9, 23, 19, 0))
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "closed")
    raw_utc_midnight = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)  # eski konvansiyon
    canonical = session_ts(date(2026, 9, 22))  # 2026-09-21T21:00Z, daha eski
    # ts DESC: ham UTC once (daha yeni), kanonik sonra gelir.
    fake_db.queue_fetchall([
        (TICKER, "1d", raw_utc_midnight, 999.0),
        (TICKER, "1d", canonical, 100.0),
        _daily(date(2026, 9, 21), 90.0),
    ])

    quote = (await quote_module.get_quotes(["ASELS"]))["ASELS"]

    assert quote["price"] == 100.0  # kanonik satir kazanir, ham 999 degil
    assert quote["previous_close"] == 90.0
    assert quote["as_of"] == canonical.isoformat()


# ---------------------------------------------------------------------------
# (3) Komsu olmayan onceki seans: previous_close None, profil karistirilmaz.
# ---------------------------------------------------------------------------


async def test_non_adjacent_previous_session_is_none_and_profile_not_mixed(fake_db, fake_redis, monkeypatch):
    _freeze_now(monkeypatch, _ist(2026, 9, 23, 19, 0))
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "closed")
    # 09-22 ile 09-17 arasinda 09-18 ve 09-21 eksik: onceki seans komsu degil.
    fake_db.queue_fetchall([
        _daily(date(2026, 9, 22), 100.0),
        _daily(date(2026, 9, 17), 50.0),
    ])
    await fake_redis.set(
        "ASELS.IS",
        json.dumps({"market": {"currentPrice": 777.0, "previousClose": 111.0}}),
    )

    quote = (await quote_module.get_quotes(["ASELS"]))["ASELS"]

    assert quote["price"] == 100.0
    assert quote["previous_close"] is None  # profil previousClose'u karistirilmaz
    assert quote["change_pct"] is None
    assert quote["previous_close_as_of"] is None


# ---------------------------------------------------------------------------
# (4) Hic DB seansi yok: profil cifti birlikte kullanilir, is_stale True.
# ---------------------------------------------------------------------------


async def test_no_db_sessions_uses_profile_pair_and_is_stale(fake_db, fake_redis, monkeypatch):
    _freeze_now(monkeypatch, _ist(2026, 9, 23, 19, 0))
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "closed")
    fake_db.queue_fetchall([])
    await fake_redis.set(
        "ASELS.IS",
        json.dumps({"market": {"currentPrice": 250.0, "previousClose": 240.0}}),
    )

    quote = (await quote_module.get_quotes(["ASELS"]))["ASELS"]

    assert quote["price"] == 250.0
    assert quote["previous_close"] == 240.0
    assert quote["change_pct"] == pytest.approx((250.0 - 240.0) / 240.0 * 100, abs=1e-4)
    assert quote["is_stale"] is True


# ---------------------------------------------------------------------------
# Acik piyasa: fiyat intraday'den, onceki kapanis yalnizca komsu seanstan.
# ---------------------------------------------------------------------------


async def test_open_uses_latest_intraday_and_adjacent_previous_session(fake_db, fake_redis, monkeypatch):
    now = _ist(2026, 9, 23, 12, 0)
    _freeze_now(monkeypatch, now)
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "open")
    fake_db.queue_fetchall([
        (TICKER, "5m", now - timedelta(minutes=5), 105.0),
        (TICKER, "5m", now - timedelta(minutes=10), 104.0),
        _daily(date(2026, 9, 22), 100.0),
        _daily(date(2026, 9, 21), 90.0),
    ])

    quote = (await quote_module.get_quotes(["ASELS"]))["ASELS"]

    assert quote["market_status"] == "open"
    assert quote["price"] == 105.0
    assert quote["previous_close"] == 100.0
    assert quote["change_pct"] == pytest.approx(5.0, rel=1e-6)
    assert quote["is_stale"] is False
    assert quote["change_window"] == "previous_session_close"


async def test_open_with_non_adjacent_previous_session_yields_none(fake_db, fake_redis, monkeypatch):
    now = _ist(2026, 9, 23, 12, 0)
    _freeze_now(monkeypatch, now)
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "open")
    fake_db.queue_fetchall([
        (TICKER, "5m", now - timedelta(minutes=5), 105.0),
        _daily(date(2026, 9, 17), 50.0),
    ])

    quote = (await quote_module.get_quotes(["ASELS"]))["ASELS"]

    assert quote["price"] == 105.0
    assert quote["previous_close"] is None
    assert quote["change_pct"] is None
    assert quote["is_stale"] is False


# ---------------------------------------------------------------------------
# Sorgu zaman penceresi (tum gecmisi taramayi onler).
# ---------------------------------------------------------------------------


async def test_get_quotes_query_is_time_bounded(fake_db, fake_redis, monkeypatch):
    _freeze_now(monkeypatch, _ist(2026, 9, 23, 19, 0))
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "closed")
    fake_db.queue_fetchall([])

    await quote_module.get_quotes(["ASELS"])

    query, params = next(q for q in fake_db.queries if "FROM price_candles" in q[0])
    assert "interval = '1d' AND ts >= %s" in query
    assert "interval IN ('5m', '30m', '1h') AND ts >= %s" in query
    assert params[0] == "ASELS.IS"
    assert len(params) == 3  # ticker + 1d kesme + intraday kesme
    assert params[1] < params[2]  # 1d penceresi intraday'den daha geriye


# ---------------------------------------------------------------------------
# Kucuk istek: eksik gunluk seans telafi edilir ve SELECT tekrarlanir.
# Buyuk liste: ag cagrisi YAPILMAZ (telafi cron'a birakilir).
# ---------------------------------------------------------------------------


async def test_small_batch_ensures_fresh_daily_and_requeries(fake_db, fake_redis, monkeypatch):
    _freeze_now(monkeypatch, _ist(2026, 9, 23, 19, 0))  # kapanistan sonra
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "closed")
    from src.services import price as price_module

    # Ilk SELECT bayat (09-22) doner; telafi sonrasi tekrar SELECT taze (09-23).
    fake_db.queue_fetchall(
        [_daily(date(2026, 9, 22), 100.0)],
        [_daily(date(2026, 9, 23), 110.0)],
    )
    calls = []

    async def fake_ensure(ticker):
        calls.append(ticker)
        return True

    monkeypatch.setattr(price_module, "ensure_recent_daily_candle", fake_ensure)

    quote = (await quote_module.get_quotes(["ASELS"]))["ASELS"]

    assert calls == ["ASELS"]
    assert quote["price"] == 110.0
    assert quote["is_stale"] is False
    selects = [q for q in fake_db.queries if "FROM price_candles" in q[0]]
    assert len(selects) == 2  # telafi sonrasi yeniden sorgu


async def test_large_batch_does_not_ensure(fake_db, fake_redis, monkeypatch):
    _freeze_now(monkeypatch, _ist(2026, 9, 23, 19, 0))
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "closed")
    from src.services import price as price_module

    tickers = [f"T{i:02d}" for i in range(26)]  # > _ENSURE_FRESH_MAX_TICKERS
    fake_db.queue_fetchall([])
    calls = []

    async def fake_ensure(ticker):
        calls.append(ticker)
        return False

    monkeypatch.setattr(price_module, "ensure_recent_daily_candle", fake_ensure)

    await quote_module.get_quotes(tickers)

    assert calls == []
    selects = [q for q in fake_db.queries if "FROM price_candles" in q[0]]
    assert len(selects) == 1  # telafi/re-sorgu YOK
