"""Mum yazim yollarinin kanoniklik/placeholder butunlugu testleri.

Uc yazim yolu da ayni iki saf yardimciyi uygulamalidir (``src.services.market``):
- ``normalize_candle_ts`` gunluk-ve-uzeri araliklari kanonik Istanbul gece
  yarisina cevirir (intraday'a dokunmaz),
- ``is_placeholder_candle`` halt edilmis sembolun duz/hacimsiz satirini eler.

Tamamen hermetik: ag (yfinance) monkeypatch ile kesilir; DB/Redis
``fake_db``/``fake_redis`` ile karsilanir. ``_build_candle_rows`` hic DB
kullanmaz (saf satir uretimi), ``_update_batch`` ise ``executemany`` ile
yazar — bu yuzden yazilan satirlar ``fake_db.queries``'ten okunur.
"""

from datetime import date, datetime, timezone

import pandas as pd

import src.cron.tasks as tasks_module
import src.services.bulk as bulk_module
import src.services.price as price_module
from src.services.market import session_date, session_ts

# Gunluk on-demand yol tz-aware zaman damgasi uretir (ornek: Istanbul gece
# yarisina denk gelen 21:00Z). Placeholder satir: hacim 0 ve O=H=L=C.
AWARE_NORMAL_TS = pd.Timestamp("2026-08-19 21:00:00", tz="UTC")
AWARE_PLACEHOLDER_TS = pd.Timestamp("2026-08-20 21:00:00", tz="UTC")


def _daily_frame(index, rows):
    """OHLCV kolonlu kucuk bir gunluk DataFrame uretir."""
    return pd.DataFrame(
        {
            "Open": [r[0] for r in rows],
            "High": [r[1] for r in rows],
            "Low": [r[2] for r in rows],
            "Close": [r[3] for r in rows],
            "Volume": [r[4] for r in rows],
        },
        index=index,
    )


# ---------------------------------------------------------------------------
# price._build_candle_rows (on-demand yol)
# ---------------------------------------------------------------------------


async def test_build_candle_rows_normalizes_and_drops_placeholder(monkeypatch):
    """Tz-aware gunluk seride normal satir Istanbul gece yarisina normalize
    edilir, placeholder satir (hacim 0 + O=H=L=C) atilir."""
    data = _daily_frame(
        pd.DatetimeIndex([AWARE_NORMAL_TS, AWARE_PLACEHOLDER_TS]),
        [
            (10.0, 11.0, 9.5, 10.5, 1000),
            (7.5, 7.5, 7.5, 7.5, 0),
        ],
    )

    async def fake_fetch(ticker, interval, start, end):
        return data

    monkeypatch.setattr(price_module, "afetch_price_history", fake_fetch)

    rows = await price_module._build_candle_rows(
        "THYAO.IS", "1d",
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    assert len(rows) == 1
    ticker, interval, ts, open_, high, low, close, volume = rows[0]
    assert (ticker, interval) == ("THYAO.IS", "1d")
    assert ts == session_ts(session_date(AWARE_NORMAL_TS.to_pydatetime()))
    assert (open_, high, low, close, volume) == (10.0, 11.0, 9.5, 10.5, 1000)


async def test_build_candle_rows_leaves_intraday_ts_untouched(monkeypatch):
    """Intraday aralikta normalize no-op'tur: ts oldugu gibi kalir."""
    intraday_ts = pd.Timestamp("2026-08-20 13:30:00", tz="Europe/Istanbul")
    data = _daily_frame(pd.DatetimeIndex([intraday_ts]), [(10.0, 11.0, 9.5, 10.5, 1000)])

    async def fake_fetch(ticker, interval, start, end):
        return data

    monkeypatch.setattr(price_module, "afetch_price_history", fake_fetch)

    rows = await price_module._build_candle_rows(
        "THYAO.IS", "1h",
        datetime(2026, 8, 20, tzinfo=timezone.utc),
        datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert len(rows) == 1
    assert rows[0][2] == intraday_ts.to_pydatetime()


# ---------------------------------------------------------------------------
# cron tasks._update_batch (yf.download batch yolu, tz-naive)
# ---------------------------------------------------------------------------


async def test_update_batch_writes_canonical_session_ts(monkeypatch, fake_db, fake_redis):
    """tz-naive (yf.download) gunluk ts, UTC varsayilmadan seans tarihinin
    kanonik Istanbul gece yarisina yazilir."""
    idx = pd.DatetimeIndex([pd.Timestamp("2026-08-20")])
    cols = pd.MultiIndex.from_tuples([
        ("THYAO.IS", "Open"),
        ("THYAO.IS", "High"),
        ("THYAO.IS", "Low"),
        ("THYAO.IS", "Close"),
        ("THYAO.IS", "Volume"),
    ])
    data = pd.DataFrame([[10.0, 11.0, 9.5, 10.5, 1000]], index=idx, columns=cols)

    async def fake_download(batch_tickers, period, interval):
        return data

    monkeypatch.setattr(tasks_module, "_download_prices", fake_download)

    await tasks_module._update_batch(["THYAO.IS"], "1d", "1mo", "rest", 0, 1)

    inserts = [params for query, params in fake_db.queries if "INSERT INTO price_candles" in query]
    assert inserts, "price_candles INSERT bekleniyordu"
    values = inserts[0]
    assert len(values) == 1
    ticker, interval, ts, open_, high, low, close, volume = values[0]
    assert (ticker, interval) == ("THYAO.IS", "1d")
    assert ts == session_ts(date(2026, 8, 20))
    assert (open_, high, low, close, volume) == (10.0, 11.0, 9.5, 10.5, 1000)


# ---------------------------------------------------------------------------
# bulk.build_candle_rows_bulk (yillik bulk fill yolu)
# ---------------------------------------------------------------------------


async def test_build_candle_rows_bulk_normalizes_and_drops_placeholder(monkeypatch):
    """Bulk yolunda da gunluk ts kanoniklesir ve placeholder satir atilir."""
    data = _daily_frame(
        pd.DatetimeIndex([AWARE_NORMAL_TS, AWARE_PLACEHOLDER_TS]),
        [
            (10.0, 11.0, 9.5, 10.5, 1000),
            (7.5, 7.5, 7.5, 7.5, 0),
        ],
    )

    def fake_download(tickers_is, start, end, interval):
        return data

    monkeypatch.setattr(bulk_module, "_download_sync", fake_download)

    rows = await bulk_module.build_candle_rows_bulk(
        ["THYAO.IS"],
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2027, 1, 1, tzinfo=timezone.utc),
        "1d",
    )

    assert len(rows) == 1
    ticker, interval, ts, open_, high, low, close, volume = rows[0]
    assert (ticker, interval) == ("THYAO.IS", "1d")
    assert ts == session_ts(session_date(AWARE_NORMAL_TS.to_pydatetime()))
    assert (open_, high, low, close, volume) == (10.0, 11.0, 9.5, 10.5, 1000)
