"""Unit tests for src/services/credits.py.

Hermetic: the shared async ``db`` singleton is swapped for ``FakeDB`` (the
``fake_db`` fixture). Credit flows are exercised by driving the cursor's
``fetchone`` result queue, so no real Postgres connection is ever opened.
"""

import pytest

import src.services.credits as credits_module


# ---------------------------------------------------------------------------
# env-driven helpers
# ---------------------------------------------------------------------------


def test_max_free_and_daily_refill_env(monkeypatch):
    monkeypatch.setenv("FREE_CREDIT_MAX", "40")
    monkeypatch.setenv("DAILY_FREE_CREDIT_REFILL", "10")
    assert credits_module._get_max_free() == 40
    assert credits_module._get_daily_refill() == 10


def test_default_credits_prefers_env(monkeypatch):
    monkeypatch.setenv("DEFAULT_CREDITS", "150")
    assert credits_module._get_default_credits() == 150


def test_default_credits_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("DEFAULT_CREDITS", raising=False)
    assert credits_module._get_default_credits() == 100


# ---------------------------------------------------------------------------
# _resolve_owner / get_total
# ---------------------------------------------------------------------------


async def test_get_total_uses_same_user_for_non_bot(fake_db):
    fake_db.queue_fetchone(None, (120.0,))
    total = await credits_module.get_total(7)
    assert total == 120.0
    sums = [q for q in fake_db.queries if "COALESCE(SUM(amount), 0)" in q[0]]
    assert sums[-1][1] == (7,)


async def test_get_total_resolves_bot_owner(fake_db):
    fake_db.queue_fetchone(("bot", 42), (55.0,))
    total = await credits_module.get_total(7)
    assert total == 55.0
    sums = [q for q in fake_db.queries if "COALESCE(SUM(amount), 0)" in q[0]]
    assert sums[-1][1] == (42,)


# ---------------------------------------------------------------------------
# spend
# ---------------------------------------------------------------------------


async def test_spend_success_from_free_credits(fake_db):
    fake_db.queue_fetchone(None, (25.0,), None, (20.0,))
    ok, remaining = await credits_module.spend(7, 5.0)
    assert ok is True
    assert remaining == 20.0
    assert fake_db.commit_calls >= 1
    assert fake_db.rollback_calls == 0
    updates = [q for q in fake_db.queries if q[0].strip().startswith("UPDATE user_credits")]
    assert len(updates) == 1
    assert updates[0][1][2] == "free_credits"
    assert updates[0][1][0] == 5.0


async def test_spend_uses_gift_after_free_exhausted(fake_db):
    fake_db.queue_fetchone(None, None, (5.0,), None, (5.0,))
    ok, remaining = await credits_module.spend(7, 5.0)
    assert ok is True
    assert remaining == 5.0
    assert fake_db.commit_calls >= 1
    updates = [q for q in fake_db.queries if q[0].strip().startswith("UPDATE user_credits")]
    assert len(updates) == 2
    assert updates[0][1][2] == "free_credits"
    assert updates[1][1][2] == "gift_credits"


async def test_spend_insufficient_rolls_back(fake_db):
    fake_db.queue_fetchone(None, None, None, None, (3.0,))
    ok, remaining = await credits_module.spend(7, 999.0)
    assert ok is False
    assert remaining == 3.0
    assert fake_db.rollback_calls == 1
    assert fake_db.commit_calls == 0


# ---------------------------------------------------------------------------
# refund / add_free_credits / add_gift_credits
# ---------------------------------------------------------------------------


async def test_refund_upserts_free_credits(fake_db):
    fake_db.queue_fetchone(None)
    await credits_module.refund(7, 10.0)
    assert fake_db.commit_calls == 1
    inserts = [q for q in fake_db.queries if "INSERT INTO user_credits" in q[0]]
    assert len(inserts) == 1
    assert "free_credits" in inserts[0][0]
    assert inserts[0][1][0] == 7
    assert inserts[0][1][1] == 10.0


async def test_add_free_credits(fake_db):
    fake_db.queue_fetchone(None)
    await credits_module.add_free_credits(7, 15.0)
    assert fake_db.commit_calls == 1
    inserts = [q for q in fake_db.queries if "INSERT INTO user_credits" in q[0]]
    assert "free_credits" in inserts[-1][0]
    assert inserts[-1][1][0] == 7
    assert inserts[-1][1][1] == 15.0


async def test_add_gift_credits(fake_db):
    fake_db.queue_fetchone(None)
    await credits_module.add_gift_credits(7, 25.0)
    assert fake_db.commit_calls == 1
    inserts = [q for q in fake_db.queries if "INSERT INTO user_credits" in q[0]]
    assert "gift_credits" in inserts[-1][0]
    assert inserts[-1][1][0] == 7
    assert inserts[-1][1][1] == 25.0


# ---------------------------------------------------------------------------
# daily_refill
# ---------------------------------------------------------------------------


async def test_daily_refill_returns_rowcount(fake_db):
    fake_db.rowcount = 5
    n = await credits_module.daily_refill()
    assert n == 5
    assert fake_db.commit_calls == 1
    inserts = [q for q in fake_db.queries if "INSERT INTO user_credits" in q[0]]
    assert len(inserts) == 1


# ---------------------------------------------------------------------------
# init_user_credits
# ---------------------------------------------------------------------------


async def test_init_user_credits_uses_env_default(monkeypatch, fake_db):
    monkeypatch.setenv("DEFAULT_CREDITS", "150")
    fake_db.queue_fetchone(None)
    await credits_module.init_user_credits(9)
    inserts = [q for q in fake_db.queries if "INSERT INTO user_credits" in q[0]]
    assert "free_credits" in inserts[-1][0]
    assert inserts[-1][1][0] == 9
    assert inserts[-1][1][1] == 150.0


async def test_init_user_credits_resolves_bot_owner(monkeypatch, fake_db):
    monkeypatch.setenv("DEFAULT_CREDITS", "100")
    fake_db.queue_fetchone(("bot", 42))
    await credits_module.init_user_credits(9)
    inserts = [q for q in fake_db.queries if "INSERT INTO user_credits" in q[0]]
    assert inserts[-1][1][0] == 42