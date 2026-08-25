"""Integration-style test: bulk cron refresh (update_tier) actually skips
suppressed tickers via src.services.ticker_health.filter_suppressed.

Hermetic: _ticker_sets / _needs_update / _update_batch are monkeypatched so
no real DB, Redis or yfinance call happens; only the wiring inside
update_tier (src/cron/tasks.py) is under test. fake_redis neutralizes the
cron lock acquire/release (r.set / r.delete) that update_tier always calls.
"""

import src.cron.tasks as tasks_module


async def test_update_tier_skips_suppressed_ticker(monkeypatch, fake_redis):
    async def fake_ticker_sets():
        return {"bist30": ["THYAO", "KENTK"], "popular": [], "rest": []}

    async def fake_filter_suppressed(tickers):
        # KENTK is "suppressed" — mirrors ticker_health.filter_suppressed
        # dropping a ticker whose consecutive-failure threshold was crossed.
        return [t for t in tickers if t != "KENTK"]

    async def fake_needs_update(ticker_list, now, max_age, interval="1d"):
        return ticker_list

    batches = []

    async def fake_update_batch(batch_tickers, interval, period, tier_name, offset, total):
        batches.append(list(batch_tickers))

    monkeypatch.setattr(tasks_module, "_ticker_sets", fake_ticker_sets)
    monkeypatch.setattr(tasks_module, "filter_suppressed", fake_filter_suppressed)
    monkeypatch.setattr(tasks_module, "_needs_update", fake_needs_update)
    monkeypatch.setattr(tasks_module, "_update_batch", fake_update_batch)

    await tasks_module.update_tier("bist30", no_lock=True)

    assert batches, "expected at least one batch to be fetched"
    all_tickers_fetched = [t for batch in batches for t in batch]
    assert "KENTK.IS" not in all_tickers_fetched
    assert "THYAO.IS" in all_tickers_fetched


async def test_update_tier_fetches_everything_when_nothing_suppressed(monkeypatch, fake_redis):
    async def fake_ticker_sets():
        return {"bist30": ["THYAO", "KENTK"], "popular": [], "rest": []}

    async def fake_filter_suppressed(tickers):
        return tickers  # nothing suppressed

    async def fake_needs_update(ticker_list, now, max_age, interval="1d"):
        return ticker_list

    batches = []

    async def fake_update_batch(batch_tickers, interval, period, tier_name, offset, total):
        batches.append(list(batch_tickers))

    monkeypatch.setattr(tasks_module, "_ticker_sets", fake_ticker_sets)
    monkeypatch.setattr(tasks_module, "filter_suppressed", fake_filter_suppressed)
    monkeypatch.setattr(tasks_module, "_needs_update", fake_needs_update)
    monkeypatch.setattr(tasks_module, "_update_batch", fake_update_batch)

    await tasks_module.update_tier("bist30", no_lock=True)

    all_tickers_fetched = [t for batch in batches for t in batch]
    assert "KENTK.IS" in all_tickers_fetched
    assert "THYAO.IS" in all_tickers_fetched
