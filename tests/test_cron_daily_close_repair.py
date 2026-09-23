"""``src/cron/tasks.py`` gunluk kapanis telafi turu icin hermetik testler.

DB ``fake_db``, kilit ``fake_redis`` ile karsilanir; ``_repair_now_ist``
sabit bir ana dondurulur, boylece grace penceresi ve beklenen seans tarihi
takvim gunune bagli kalmaz. Ag (yfinance) ``_update_batch`` monkeypatch'i
ile kesilir.
"""

from datetime import date

import src.cron.tasks as tasks_module
from src.services.market import MARKET_TIMEZONE, session_ts


def _ist(y, m, d, h, mi):
    from datetime import datetime

    return datetime(y, m, d, h, mi, tzinfo=MARKET_TIMEZONE)


def test_tasks_module_does_not_shadow_stdlib_time():
    """``from datetime import time`` stdlib ``time`` modulunu golgelememeli.

    ``run_warm_price_cache`` icindeki ``time.time()`` cagrilari aksi halde
    ``AttributeError`` verir (bu tam olarak bir kez yasandi); bu guard o
    sinifi yakalar.
    """
    import time as stdlib_time

    assert tasks_module.time is stdlib_time


# ---------------------------------------------------------------------------
# _tickers_missing_daily
# ---------------------------------------------------------------------------


async def test_tickers_missing_daily_filters_by_expected_session(fake_db, fake_redis):
    expected = date(2026, 8, 24)
    fake_db.queue_fetchall([
        ("AAA.IS", session_ts(expected)),          # guncel -> eksik degil
        ("BBB.IS", session_ts(date(2026, 8, 21))),  # geride  -> eksik
        # CCC hic yok -> eksik
    ])

    missing = await tasks_module._tickers_missing_daily(["aaa", "bbb", "ccc"], expected)

    assert missing == ["BBB", "CCC"]
    query, params = next(q for q in fake_db.queries if "MAX(ts)" in q[0])
    assert params == ["AAA.IS", "BBB.IS", "CCC.IS"]


async def test_tickers_missing_daily_empty_input_is_noop(fake_db, fake_redis):
    assert await tasks_module._tickers_missing_daily([], date(2026, 8, 24)) == []
    assert fake_db.queries == []


# ---------------------------------------------------------------------------
# run_daily_close_repair
# ---------------------------------------------------------------------------


async def test_run_daily_close_repair_noop_when_none_missing(monkeypatch, fake_db, fake_redis):
    fixed = _ist(2026, 8, 24, 19, 0)  # Pazartesi, grace sonrasi
    monkeypatch.setattr(tasks_module, "_repair_now_ist", lambda: fixed)

    async def fake_companies():
        return [{"ticker": "AAA"}]

    async def fake_filter(tickers):
        return tickers

    async def fake_missing(tickers, expected):
        return []

    update_calls = []

    async def fake_update_batch(*args, **kwargs):
        update_calls.append(args)

    monkeypatch.setattr(tasks_module, "get_bist_companies_as_dict_from_redis", fake_companies)
    monkeypatch.setattr(tasks_module, "filter_suppressed", fake_filter)
    monkeypatch.setattr(tasks_module, "_tickers_missing_daily", fake_missing)
    monkeypatch.setattr(tasks_module, "_update_batch", fake_update_batch)

    await tasks_module.run_daily_close_repair()

    assert update_calls == []
    # Bos liste ucuz no-op: lock hic alinmamali.
    assert await fake_redis.get("lock:cron:daily_close_repair") is None


async def test_run_daily_close_repair_skips_grace_window(monkeypatch, fake_db, fake_redis):
    fixed = _ist(2026, 8, 24, 18, 20)  # Pazartesi, 18:10-18:35 grace penceresi
    monkeypatch.setattr(tasks_module, "_repair_now_ist", lambda: fixed)
    called = []

    async def fake_companies():
        called.append("companies")
        return []

    monkeypatch.setattr(tasks_module, "get_bist_companies_as_dict_from_redis", fake_companies)

    await tasks_module.run_daily_close_repair()

    assert called == []
    assert await fake_redis.get("lock:cron:daily_close_repair") is None


async def test_run_daily_close_repair_batches_missing(monkeypatch, fake_db, fake_redis):
    fixed = _ist(2026, 8, 24, 19, 0)
    monkeypatch.setattr(tasks_module, "_repair_now_ist", lambda: fixed)
    monkeypatch.setattr(tasks_module, "BATCH_DELAY", 0)

    async def fake_companies():
        return [{"ticker": "AAA"}]

    async def fake_filter(tickers):
        return tickers

    missing = [f"T{i:03d}" for i in range(120)]

    async def fake_missing(tickers, expected):
        assert expected == date(2026, 8, 24)
        return missing

    calls = []

    async def fake_update_batch(batch, interval, period, tier, offset, total):
        calls.append((list(batch), interval, period, tier, offset, total))

    monkeypatch.setattr(tasks_module, "get_bist_companies_as_dict_from_redis", fake_companies)
    monkeypatch.setattr(tasks_module, "filter_suppressed", fake_filter)
    monkeypatch.setattr(tasks_module, "_tickers_missing_daily", fake_missing)
    monkeypatch.setattr(tasks_module, "_update_batch", fake_update_batch)

    await tasks_module.run_daily_close_repair()

    assert [c[4] for c in calls] == [0, 50, 100]
    assert {c[5] for c in calls} == {120}
    for _batch, interval, period, tier, _offset, _total in calls:
        assert (interval, period, tier) == ("1d", "5d", "DAILY-REPAIR")
    assert [c[0] for c in calls] == [
        [f"{t}.IS" for t in missing[0:50]],
        [f"{t}.IS" for t in missing[50:100]],
        [f"{t}.IS" for t in missing[100:120]],
    ]
    assert await fake_redis.get("lock:cron:daily_close_repair") is None
