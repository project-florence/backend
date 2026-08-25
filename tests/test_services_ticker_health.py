"""Unit tests for src/services/ticker_health.py (dead-ticker suppression).

Hermetic: the shared async ``db`` singleton is swapped for ``FakeDB`` (the
``fake_db`` fixture from conftest.py); ``fetchone``/``fetchall`` results are
driven explicitly so no real Postgres connection is ever opened.

Covers the four behaviours required by the dead-ticker suppression design:
  1. crossing the failure threshold suppresses a ticker
  2. a suppressed ticker is skipped by bulk refresh (``filter_suppressed`` /
     ``update_tier``)
  3. a suppressed ticker can recover (cooldown expiry, explicit success)
  4. storage unavailable -> nothing is suppressed (fail safe)
"""

from datetime import datetime, timedelta, timezone

import pytest

import src.services.ticker_health as ticker_health


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------


def test_classify_error_recognizes_not_found_evidence():
    assert ticker_health.classify_error("possibly delisted; no price data found") == ticker_health.NOT_FOUND
    assert ticker_health.classify_error("Quote not found for symbol: KENTK.IS (404)") == ticker_health.NOT_FOUND
    assert ticker_health.classify_error(Exception("HTTP Error 404: Not Found")) == ticker_health.NOT_FOUND


def test_classify_error_defaults_to_transient():
    assert ticker_health.classify_error("Connection reset by peer") == ticker_health.TRANSIENT
    assert ticker_health.classify_error(TimeoutError("timed out")) == ticker_health.TRANSIENT
    assert ticker_health.classify_error(None) == ticker_health.TRANSIENT


# ---------------------------------------------------------------------------
# _compute_suppression / threshold crossing (pure, no DB)
# ---------------------------------------------------------------------------


def test_compute_suppression_below_threshold_returns_none():
    now = datetime.now(timezone.utc)
    assert ticker_health._compute_suppression(1, ticker_health.NOT_FOUND, now) is None
    assert ticker_health._compute_suppression(2, ticker_health.NOT_FOUND, now) is None


def test_compute_suppression_crossing_threshold_suppresses():
    now = datetime.now(timezone.utc)
    until = ticker_health._compute_suppression(3, ticker_health.NOT_FOUND, now)
    assert until is not None
    assert until > now


def test_compute_suppression_distinguishes_kind_cooldowns():
    """not_found (strong delisting evidence) gets a longer cooldown than transient."""
    now = datetime.now(timezone.utc)
    not_found_until = ticker_health._compute_suppression(3, ticker_health.NOT_FOUND, now)
    transient_until = ticker_health._compute_suppression(3, ticker_health.TRANSIENT, now)
    assert not_found_until - now > transient_until - now


# ---------------------------------------------------------------------------
# record_failure: threshold crossing writes a suppression window
# ---------------------------------------------------------------------------


async def test_record_failure_crossing_threshold_sets_suppressed_until(fake_db):
    # Ticker already has 2 consecutive failures on file.
    fake_db.queue_fetchone((2,))
    await ticker_health.record_failure("KENTK", ticker_health.NOT_FOUND, "possibly delisted")

    upserts = [q for q in fake_db.queries if q[0].strip().startswith("INSERT INTO ticker_health")]
    assert len(upserts) == 1
    params = upserts[0][1]
    # (ticker, consecutive_failures, last_attempt_at, kind, error, suppressed_until, updated_at)
    assert params[0] == "KENTK"
    assert params[1] == 3
    assert params[5] is not None  # suppressed_until set
    assert fake_db.commit_calls == 1
    assert fake_db.rollback_calls == 0


async def test_record_failure_below_threshold_leaves_suppressed_until_null(fake_db):
    fake_db.queue_fetchone(None)  # no prior row
    await ticker_health.record_failure("YKB", ticker_health.TRANSIENT, "timeout")

    upserts = [q for q in fake_db.queries if q[0].strip().startswith("INSERT INTO ticker_health")]
    params = upserts[0][1]
    assert params[1] == 1
    assert params[5] is None


# ---------------------------------------------------------------------------
# get_suppressed_tickers / filter_suppressed: skip + recovery
# ---------------------------------------------------------------------------


async def test_get_suppressed_tickers_excludes_expired_and_includes_active(fake_db):
    now = datetime.now(timezone.utc)
    fake_db.queue_fetchall([
        ("KENTK", now + timedelta(days=1)),   # still suppressed
        ("YKB", now - timedelta(hours=1)),    # cooldown expired -> recovered
    ])
    suppressed = await ticker_health.get_suppressed_tickers()
    assert suppressed == {"KENTK"}


async def test_filter_suppressed_skips_active_suppression(fake_db):
    now = datetime.now(timezone.utc)
    fake_db.queue_fetchall([("KENTK", now + timedelta(days=1))])
    result = await ticker_health.filter_suppressed(["KENTK", "THYAO"])
    assert result == ["THYAO"]


async def test_filter_suppressed_lets_recovered_ticker_through(fake_db):
    """A ticker whose cooldown has expired is retried again (recovery)."""
    now = datetime.now(timezone.utc)
    fake_db.queue_fetchall([("KENTK", now - timedelta(minutes=1))])
    result = await ticker_health.filter_suppressed(["KENTK", "THYAO"])
    assert result == ["KENTK", "THYAO"]


async def test_record_success_clears_suppression(fake_db):
    await ticker_health.record_success("KENTK")
    upserts = [q for q in fake_db.queries if q[0].strip().startswith("INSERT INTO ticker_health")]
    assert len(upserts) == 1
    params = upserts[0][1]
    # (ticker, last_attempt_at, last_success_at, updated_at)
    assert params[0] == "KENTK"
    assert fake_db.commit_calls == 1


# ---------------------------------------------------------------------------
# Fail-safe: storage unavailable -> nothing suppressed
# ---------------------------------------------------------------------------


async def test_get_suppressed_tickers_fails_safe_when_db_unavailable(monkeypatch):
    from src.core import database as db_module

    def _broken_cursor(*args, **kwargs):
        raise ConnectionError("db unreachable")

    monkeypatch.setattr(db_module.db, "cursor", _broken_cursor)
    result = await ticker_health.get_suppressed_tickers()
    assert result == set()


async def test_filter_suppressed_attempts_everything_when_db_unavailable(monkeypatch):
    from src.core import database as db_module

    def _broken_cursor(*args, **kwargs):
        raise ConnectionError("db unreachable")

    monkeypatch.setattr(db_module.db, "cursor", _broken_cursor)
    tickers = ["KENTK", "THYAO", "YKB"]
    result = await ticker_health.filter_suppressed(tickers)
    assert result == tickers


async def test_record_failure_does_not_raise_when_db_unavailable(monkeypatch):
    from src.core import database as db_module

    def _broken_cursor(*args, **kwargs):
        raise ConnectionError("db unreachable")

    monkeypatch.setattr(db_module.db, "cursor", _broken_cursor)
    # Must not raise -- a broken health-tracking store must never crash the
    # cron round that is trying to fetch prices.
    await ticker_health.record_failure("KENTK", ticker_health.NOT_FOUND, "boom")
