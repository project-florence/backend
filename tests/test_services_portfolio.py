"""Unit tests for src/services/portfolio.py.

Hermetic: the shared async ``db`` singleton is swapped for ``FakeDB`` (the
``fake_db`` fixture), and all external data sources (market status, ticker
validation, current prices, price history, currency data) are stubbed. No
Postgres/Redis/network access happens.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import src.services.economy as economy_module
import src.services.portfolio as portfolio_module
import src.services.ticker as ticker_module
from src.services.portfolio import Metadata, Portfolio, Transaction


def _now():
    return datetime.now(timezone.utc)


def _tx(ticker, tx_type, quantity, price, commission=0.0, total=None, date=None):
    subtotal = price * quantity
    if total is None:
        total = round(
            subtotal + commission if tx_type == "BUY" else subtotal - commission,
            2,
        )
    return Transaction(
        id=f"tx-{uuid.uuid4()}",
        ticker=ticker,
        type=tx_type,
        quantity=quantity,
        price=price,
        commission=commission,
        total=total,
        date=date or _now(),
    )


def _portfolio(**overrides):
    defaults = dict(
        id="port-test",
        user_id=1,
        name="Test",
        initial_balance=10000.0,
        balance=10000.0,
        created_at=_now() - timedelta(days=15),
        updated_at=_now() - timedelta(days=15),
    )
    defaults.update(overrides)
    meta = Metadata(**defaults)
    return Portfolio(metadata=meta)


def _saved_portfolio(fake_db) -> Portfolio:
    """Parse the last portfolio persisted through ``save_portfolio``.

    The service under test always loads a fresh copy from the DB layer before
    mutating, so the local ``Portfolio`` objects in tests are never touched.
    To assert on post-operation state, read back what was actually written in
    the final ``INSERT ... ON CONFLICT`` payload.
    """
    inserts = [q for q in fake_db.queries if "INSERT INTO portfolios" in q[0]]
    return Portfolio.model_validate_json(inserts[-1][1][2])


@pytest.fixture(autouse=True)
def _stub_external(monkeypatch):
    monkeypatch.setattr(portfolio_module, "get_market_status", lambda: "open")

    async def _valid(t):
        return True

    async def _price(t, interval="5m"):
        return 100.0

    async def _hist(t, start=None, end=None):
        return []

    monkeypatch.setattr(portfolio_module, "is_valid_ticker", _valid)
    monkeypatch.setattr(portfolio_module, "get_current_price", _price)
    monkeypatch.setattr(portfolio_module, "get_price_history", _hist)

    async def _cur():
        return {}

    monkeypatch.setattr(economy_module, "get_currency", _cur)
    monkeypatch.setattr(ticker_module, "PRECIOUS_METAL_KEYS", [])


# ---------------------------------------------------------------------------
# create / save / load / list / delete
# ---------------------------------------------------------------------------


async def test_create_portfolio(fake_db):
    p = await portfolio_module.create_portfolio(1, "Test", 10000.0)
    assert p is not None
    assert p.metadata.id.startswith("port-")
    assert p.metadata.balance == 10000.0
    assert p.metadata.user_id == 1
    assert fake_db.commit_calls >= 1
    inserts = [q for q in fake_db.queries if "INSERT INTO portfolios" in q[0]]
    assert len(inserts) == 1
    assert inserts[0][1][0] == p.metadata.id


async def test_create_portfolio_save_failure(monkeypatch, fake_db):
    from src.core import database as db_module

    async def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db_module.db, "commit", _boom)
    p = await portfolio_module.create_portfolio(1, "Test", 1000.0)
    assert p is None


async def test_save_portfolio_updates_existing(fake_db):
    p = _portfolio()
    ok = await portfolio_module.save_portfolio(p)
    assert ok is True
    assert fake_db.commit_calls >= 1
    inserts = [q for q in fake_db.queries if "INSERT INTO portfolios" in q[0]]
    assert "ON CONFLICT (portfolio_id)" in inserts[0][0]


async def test_load_portfolio(fake_db):
    p = _portfolio()
    fake_db.queue_fetchone((p.model_dump(),))
    loaded = await portfolio_module.load_portfolio("port-test", 1)
    assert loaded is not None
    assert loaded.metadata.id == "port-test"
    assert loaded.metadata.user_id == 1


async def test_load_portfolio_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.load_portfolio("port-test", 1) is None


async def test_load_portfolio_invalid_data(fake_db):
    fake_db.queue_fetchone(("not-json",))
    assert await portfolio_module.load_portfolio("port-test", 1) is None


async def test_list_portfolios(fake_db):
    p1 = _portfolio(id="port-a")
    p2 = _portfolio(id="port-b")
    fake_db.queue_fetchall([(p1.model_dump(),), (p2.model_dump(),)])
    portfolios = await portfolio_module.list_portfolios(1)
    assert [p.metadata.id for p in portfolios] == ["port-a", "port-b"]


async def test_list_portfolios_empty(fake_db):
    fake_db.queue_fetchall()
    assert await portfolio_module.list_portfolios(1) == []


async def test_delete_portfolio_success(fake_db):
    fake_db.rowcount = 1
    ok = await portfolio_module.delete_portfolio("port-test", 1)
    assert ok is True
    assert fake_db.commit_calls >= 1
    deletes = [q for q in fake_db.queries if "DELETE FROM portfolios" in q[0]]
    assert deletes[0][1] == ("port-test", 1)


async def test_delete_portfolio_not_found(fake_db):
    fake_db.rowcount = 0
    assert await portfolio_module.delete_portfolio("port-test", 1) is False


async def test_delete_portfolio_error(monkeypatch, fake_db):
    from src.core import database as db_module

    async def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db_module.db, "commit", _boom)
    assert await portfolio_module.delete_portfolio("port-test", 1) is False


# ---------------------------------------------------------------------------
# rename / get by name / duplicate
# ---------------------------------------------------------------------------


async def test_rename_portfolio(fake_db):
    p = _portfolio(name="Old")
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.rename_portfolio("port-test", 1, "New")
    assert ok is True
    assert _saved_portfolio(fake_db).metadata.name == "New"


async def test_rename_portfolio_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.rename_portfolio("port-test", 1, "New") is False


async def test_get_portfolio_by_name_case_insensitive(fake_db):
    p1 = _portfolio(id="port-a", name="Alpha")
    p2 = _portfolio(id="port-b", name="beta")

    fake_db.queue_fetchall([(p1.model_dump(),), (p2.model_dump(),)])
    found = await portfolio_module.get_portfolio_by_name(1, "ALPHA")
    assert found.metadata.id == "port-a"

    fake_db.queue_fetchall([(p1.model_dump(),), (p2.model_dump(),)])
    found = await portfolio_module.get_portfolio_by_name(1, "BETA")
    assert found.metadata.id == "port-b"

    fake_db.queue_fetchall([(p1.model_dump(),), (p2.model_dump(),)])
    assert await portfolio_module.get_portfolio_by_name(1, "none") is None


async def test_duplicate_portfolio(fake_db):
    p = _portfolio()
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))
    fake_db.queue_fetchone((p.model_dump(),))
    dup = await portfolio_module.duplicate_portfolio("port-test", 1, "Copy")
    assert dup is not None
    assert dup.metadata.id.startswith("port-")
    assert dup.metadata.id != "port-test"
    assert dup.metadata.name == "Copy"
    assert len(dup.transactions) == 1
    assert dup.transactions[0].ticker == "THYAO"


async def test_duplicate_portfolio_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.duplicate_portfolio("port-test", 1, "Copy") is None


# ---------------------------------------------------------------------------
# calculate_assets
# ---------------------------------------------------------------------------


def test_calculate_assets_buys_and_sells():
    p = _portfolio()
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))
    p.transactions.append(_tx("THYAO", "BUY", 10, 200.0, commission=1.0))
    p.transactions.append(_tx("THYAO", "SELL", 5, 150.0, commission=1.0))
    assets = portfolio_module.calculate_assets(p)
    a = assets["THYAO"]
    assert a.amount == 15.0
    assert a.weighted_price == pytest.approx(150.0)
    assert a.total_transactions == 3


def test_calculate_assets_empty_and_fully_sold():
    p = _portfolio()
    assert portfolio_module.calculate_assets(p) == {}
    p.transactions.append(_tx("THYAO", "SELL", 10, 100.0, commission=1.0))
    assert portfolio_module.calculate_assets(p) == {}


# ---------------------------------------------------------------------------
# add_transaction
# ---------------------------------------------------------------------------


async def test_add_transaction_closed_market_raises(monkeypatch, fake_db):
    monkeypatch.setattr(portfolio_module, "get_market_status", lambda: "closed")
    with pytest.raises(HTTPException) as exc:
        await portfolio_module.add_transaction("port-test", 1, "THYAO", "BUY", 10)
    assert exc.value.status_code == 400


async def test_add_transaction_zero_quantity(fake_db):
    assert await portfolio_module.add_transaction("port-test", 1, "THYAO", "BUY", 0) is False


async def test_add_transaction_invalid_ticker(monkeypatch, fake_db):
    async def _invalid(t):
        return False

    monkeypatch.setattr(portfolio_module, "is_valid_ticker", _invalid)
    assert await portfolio_module.add_transaction("port-test", 1, "THYAO", "BUY", 10) is False


async def test_add_transaction_portfolio_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.add_transaction("port-test", 1, "THYAO", "BUY", 10) is False


async def test_add_transaction_buy_success(fake_db):
    p = _portfolio(balance=10000.0)
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.add_transaction("port-test", 1, "THYAO", "BUY", 10)
    assert ok is True
    saved = _saved_portfolio(fake_db)
    tx = saved.transactions[0]
    assert tx.type == "BUY"
    assert tx.quantity == 10.0
    assert tx.price == 100.0
    assert tx.commission == 1.0
    assert tx.total == 1001.0
    assert saved.metadata.balance == pytest.approx(8999.0)


async def test_add_transaction_buy_insufficient_balance(fake_db):
    p = _portfolio(balance=500.0)
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.add_transaction("port-test", 1, "THYAO", "BUY", 10)
    assert ok is False
    assert p.transactions == []


async def test_add_transaction_sell_success(fake_db):
    p = _portfolio(balance=0.0)
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.add_transaction("port-test", 1, "THYAO", "SELL", 4)
    assert ok is True
    saved = _saved_portfolio(fake_db)
    assert saved.transactions[-1].type == "SELL"
    assert saved.metadata.balance == pytest.approx(round(4 * 100 - 0.4, 2))


async def test_add_transaction_sell_more_than_held(fake_db):
    p = _portfolio(balance=0.0)
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.add_transaction("port-test", 1, "THYAO", "SELL", 15)
    assert ok is False
    assert len(p.transactions) == 1


async def test_add_transaction_invalid_type(fake_db):
    p = _portfolio()
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.add_transaction("port-test", 1, "THYAO", "HOLD", 10)
    assert ok is False
    assert p.transactions == []


# ---------------------------------------------------------------------------
# get_transactions
# ---------------------------------------------------------------------------


async def test_get_transactions_filter_and_sort(fake_db):
    p = _portfolio()
    d1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p.transactions = [
        _tx("THYAO", "BUY", 5, 100.0, commission=1.0, date=d1),
        _tx("GARAN", "BUY", 3, 50.0, commission=1.0, date=d1 + timedelta(days=1)),
        _tx("THYAO", "SELL", 2, 120.0, commission=1.0, date=d1 + timedelta(days=2)),
    ]

    fake_db.queue_fetchone((p.model_dump(),))
    txs = await portfolio_module.get_transactions("port-test", 1, ticker="thyao")
    assert [t.ticker for t in txs] == ["THYAO", "THYAO"]

    fake_db.queue_fetchone((p.model_dump(),))
    txs = await portfolio_module.get_transactions("port-test", 1, tx_type="sell")
    assert len(txs) == 1 and txs[0].type == "SELL"

    fake_db.queue_fetchone((p.model_dump(),))
    txs = await portfolio_module.get_transactions("port-test", 1)
    assert len(txs) == 3


async def test_get_transactions_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.get_transactions("port-test", 1) is None


# ---------------------------------------------------------------------------
# get_transaction_stats
# ---------------------------------------------------------------------------


async def test_get_transaction_stats(fake_db):
    p = _portfolio()
    p.transactions = [
        _tx("THYAO", "BUY", 5, 100.0, commission=1.0),
        _tx("GARAN", "BUY", 3, 50.0, commission=1.0),
        _tx("THYAO", "SELL", 2, 120.0, commission=1.0),
    ]
    fake_db.queue_fetchone((p.model_dump(),))
    stats = await portfolio_module.get_transaction_stats("port-test", 1)
    assert stats["total_transactions"] == 3
    assert stats["total_buys"] == 2
    assert stats["total_sells"] == 1
    assert stats["unique_tickers"] == 2
    assert stats["total_buy_volume"] == pytest.approx(5 * 100 + 3 * 50)
    assert stats["total_sell_volume"] == pytest.approx(2 * 120)


async def test_get_transaction_stats_empty(fake_db):
    p = _portfolio()
    fake_db.queue_fetchone((p.model_dump(),))
    stats = await portfolio_module.get_transaction_stats("port-test", 1)
    assert stats["total_transactions"] == 0
    assert stats["unique_tickers"] == 0


async def test_get_transaction_stats_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.get_transaction_stats("port-test", 1) is None


# ---------------------------------------------------------------------------
# valuation
# ---------------------------------------------------------------------------


async def test_get_portfolio_valuation(fake_db, monkeypatch):
    p = _portfolio(initial_balance=2000.0, balance=1000.0)
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))

    async def _price(t, interval="5m"):
        return 150.0

    monkeypatch.setattr(portfolio_module, "get_current_price", _price)
    fake_db.queue_fetchone((p.model_dump(),))
    val = await portfolio_module.get_portfolio_valuation("port-test", 1)

    assert val["cash_balance"] == 1000.0
    assert val["holdings_value"] == 1500.0
    assert val["total_value"] == 2500.0
    assert val["total_pnl"] == pytest.approx(500.0)
    assert val["pnl_percentage"] == pytest.approx(25.0)
    asset = val["assets"][0]
    assert asset["ticker"] == "THYAO"
    assert asset["total_cost"] == pytest.approx(1000.0)
    assert asset["unrealized_pnl"] == pytest.approx(500.0)


async def test_get_portfolio_valuation_missing_price(fake_db, monkeypatch):
    p = _portfolio(initial_balance=2000.0, balance=1000.0)
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))

    async def _price(t, interval="5m"):
        return None

    monkeypatch.setattr(portfolio_module, "get_current_price", _price)
    fake_db.queue_fetchone((p.model_dump(),))
    val = await portfolio_module.get_portfolio_valuation("port-test", 1)
    assert val["holdings_value"] == 0.0
    assert val["total_value"] == pytest.approx(1000.0)
    assert val["assets"][0]["current_price"] is None


async def test_get_portfolio_valuation_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.get_portfolio_valuation("port-test", 1) is None


# ---------------------------------------------------------------------------
# diversification
# ---------------------------------------------------------------------------


async def test_get_diversification_stock(fake_db):
    p = _portfolio(initial_balance=2000.0, balance=500.0)
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))
    fake_db.queue_fetchone((p.model_dump(),))
    div = await portfolio_module.get_diversification("port-test", 1)
    assert div["total_value"] == pytest.approx(1500.0)
    assert div["cash_balance"] == 500.0
    assert div["assets"][0]["type"] == "stock"
    assert div["allocation_by_type"] == {"stock": 1000.0}


async def test_get_diversification_metal(fake_db, monkeypatch):
    monkeypatch.setattr(ticker_module, "PRECIOUS_METAL_KEYS", ["gumus"])
    p = _portfolio(initial_balance=2000.0, balance=500.0)
    p.transactions.append(_tx("gumus", "BUY", 10, 100.0, commission=1.0))
    fake_db.queue_fetchone((p.model_dump(),))
    div = await portfolio_module.get_diversification("port-test", 1)
    assert div["assets"][0]["type"] == "metal"


async def test_get_diversification_forex(fake_db, monkeypatch):
    async def _cur():
        return {"USD": {"Buying": 41.0}}

    monkeypatch.setattr(economy_module, "get_currency", _cur)
    p = _portfolio(initial_balance=2000.0, balance=500.0)
    p.transactions.append(_tx("USD", "BUY", 10, 100.0, commission=1.0))
    fake_db.queue_fetchone((p.model_dump(),))
    div = await portfolio_module.get_diversification("port-test", 1)
    assert div["assets"][0]["type"] == "forex"


async def test_get_diversification_empty_assets(fake_db):
    p = _portfolio(balance=500.0)
    fake_db.queue_fetchone((p.model_dump(),))
    div = await portfolio_module.get_diversification("port-test", 1)
    assert div == {"total_value": 500.0, "assets": []}


# ---------------------------------------------------------------------------
# best/worst performers
# ---------------------------------------------------------------------------


async def test_get_best_worst_performers(fake_db, monkeypatch):
    p = _portfolio(initial_balance=100000.0, balance=10000.0)
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))
    p.transactions.append(_tx("GARAN", "BUY", 10, 200.0, commission=1.0))

    async def _price(t, interval="5m"):
        return 150.0 if t == "THYAO" else 100.0

    monkeypatch.setattr(portfolio_module, "get_current_price", _price)
    fake_db.queue_fetchone((p.model_dump(),))
    res = await portfolio_module.get_best_worst_performers("port-test", 1)
    assert res["best"][0]["ticker"] == "THYAO"
    assert res["worst"][0]["ticker"] == "GARAN"


async def test_get_best_worst_performers_empty(fake_db):
    p = _portfolio()
    fake_db.queue_fetchone((p.model_dump(),))
    assert await portfolio_module.get_best_worst_performers("port-test", 1) == {"best": [], "worst": []}


# ---------------------------------------------------------------------------
# undo / update transaction
# ---------------------------------------------------------------------------


async def test_undo_last_transaction(fake_db):
    p = _portfolio(initial_balance=10000.0, balance=8999.0)
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0, total=1001.0))
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.undo_last_transaction("port-test", 1)
    assert ok is True
    saved = _saved_portfolio(fake_db)
    assert saved.transactions == []
    assert saved.metadata.balance == 10000.0


async def test_undo_last_transaction_empty(fake_db):
    p = _portfolio()
    fake_db.queue_fetchone((p.model_dump(),))
    assert await portfolio_module.undo_last_transaction("port-test", 1) is False


async def test_undo_last_transaction_recalc_fails(fake_db):
    p = _portfolio(initial_balance=10000.0)
    p.transactions = [
        _tx("THYAO", "SELL", 10, 100.0, commission=1.0),
        _tx("THYAO", "SELL", 5, 100.0, commission=1.0),
    ]
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.undo_last_transaction("port-test", 1)
    assert ok is False
    assert len(p.transactions) == 2  # re-appended


async def test_update_transaction_price(fake_db):
    p = _portfolio(initial_balance=10000.0, balance=8999.0)
    tx = _tx("THYAO", "BUY", 10, 100.0, commission=1.0, total=1001.0)
    p.transactions.append(tx)
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.update_transaction("port-test", 1, tx.id, price=90.0)
    assert ok is True
    saved = _saved_portfolio(fake_db)
    saved_tx = saved.transactions[0]
    assert saved_tx.price == 90.0
    assert saved_tx.total == pytest.approx(900.9)
    assert saved.metadata.balance == pytest.approx(9099.1)


async def test_update_transaction_not_found(fake_db):
    p = _portfolio()
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.update_transaction("port-test", 1, "nope", price=90.0)
    assert ok is False


async def test_update_transaction_reverts_on_invalid(fake_db):
    p = _portfolio(initial_balance=1000.0, balance=1000.0)
    tx = _tx("THYAO", "BUY", 5, 100.0, commission=0.5, total=500.5)
    p.transactions.append(tx)
    fake_db.queue_fetchone((p.model_dump(),))
    ok = await portfolio_module.update_transaction("port-test", 1, tx.id, quantity=100.0)
    assert ok is False
    assert tx.quantity == 5.0
    assert tx.price == 100.0


# ---------------------------------------------------------------------------
# history / returns / risk / benchmark / analysis
# ---------------------------------------------------------------------------


async def test_get_portfolio_history_empty(fake_db):
    p = _portfolio(initial_balance=1000.0, balance=1000.0)
    fake_db.queue_fetchone((p.model_dump(),))
    hist = await portfolio_module.get_portfolio_history("port-test", 1, "1mo")
    assert len(hist) == 2
    assert hist[0]["total_value"] == 1000.0
    assert hist[-1]["total_value"] == 1000.0


async def test_get_portfolio_history_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.get_portfolio_history("port-test", 1) is None


async def test_get_returns(monkeypatch, fake_db):
    p = _portfolio(initial_balance=1000.0, balance=1000.0)
    fake_db.queue_fetchone((p.model_dump(),))
    d1 = _now() - timedelta(days=365)
    d2 = _now()
    fake_history = [
        {"ts": d1.isoformat(), "total_value": 1000.0, "cash_balance": 1000.0, "holdings_value": 0.0},
        {"ts": d2.isoformat(), "total_value": 1100.0, "cash_balance": 1100.0, "holdings_value": 0.0},
    ]

    async def _hist(pf_id, user, period="1mo"):
        return fake_history

    monkeypatch.setattr(portfolio_module, "get_portfolio_history", _hist)
    res = await portfolio_module.get_returns("port-test", 1, "1y")
    assert res["start_value"] == 1000.0
    assert res["end_value"] == 1100.0
    assert res["absolute_return"] == 100.0
    assert res["total_return_percentage"] == pytest.approx(10.0)
    assert res["cagr_percentage"] is not None


async def test_get_returns_no_history(monkeypatch, fake_db):
    p = _portfolio(initial_balance=1000.0)
    fake_db.queue_fetchone((p.model_dump(),))

    async def _hist(pf_id, user, period="1mo"):
        return []

    monkeypatch.setattr(portfolio_module, "get_portfolio_history", _hist)
    assert await portfolio_module.get_returns("port-test", 1) is None


async def test_get_risk_metrics(monkeypatch, fake_db):
    p = _portfolio(initial_balance=1000.0)
    fake_db.queue_fetchone((p.model_dump(),))
    base = _now() - timedelta(days=2)
    fake_history = [
        {"ts": (base + timedelta(days=0)).isoformat(), "total_value": 100.0},
        {"ts": (base + timedelta(days=1)).isoformat(), "total_value": 110.0},
        {"ts": (base + timedelta(days=2)).isoformat(), "total_value": 99.0},
    ]

    async def _hist(pf_id, user, period="1mo"):
        return fake_history

    monkeypatch.setattr(portfolio_module, "get_portfolio_history", _hist)
    res = await portfolio_module.get_risk_metrics("port-test", 1)
    assert res["volatility"] == pytest.approx(10.0)
    assert res["max_drawdown"] == pytest.approx(10.0)
    assert res["sharpe_ratio"] == pytest.approx(0.0)


async def test_get_risk_metrics_short_history(monkeypatch, fake_db):
    p = _portfolio(initial_balance=1000.0)
    fake_db.queue_fetchone((p.model_dump(),))

    async def _hist(pf_id, user, period="1mo"):
        return [{"ts": _now().isoformat(), "total_value": 100.0}]

    monkeypatch.setattr(portfolio_module, "get_portfolio_history", _hist)
    res = await portfolio_module.get_risk_metrics("port-test", 1)
    assert res == {"volatility": None, "max_drawdown": None, "sharpe_ratio": None}


async def test_compare_with_benchmark(monkeypatch, fake_db):
    p = _portfolio(initial_balance=1000.0)
    fake_db.queue_fetchone((p.model_dump(),))
    d1 = _now() - timedelta(days=30)
    d2 = _now()
    fake_history = [
        {"ts": d1.isoformat(), "total_value": 1000.0, "cash_balance": 1000.0, "holdings_value": 0.0},
        {"ts": d2.isoformat(), "total_value": 1100.0, "cash_balance": 1100.0, "holdings_value": 0.0},
    ]

    async def _hist(pf_id, user, period="1mo"):
        return fake_history

    async def _bench_hist(ticker, start=None, end=None):
        return [
            {"ts": d1.isoformat(), "price": 100.0},
            {"ts": d2.isoformat(), "price": 110.0},
        ]

    monkeypatch.setattr(portfolio_module, "get_portfolio_history", _hist)
    monkeypatch.setattr(portfolio_module, "get_price_history", _bench_hist)
    res = await portfolio_module.compare_with_benchmark("port-test", 1, "XU100")
    assert res["portfolio_return_pct"] == pytest.approx(10.0)
    assert res["benchmark_return_pct"] == pytest.approx(10.0)
    assert res["difference_pct"] == pytest.approx(0.0)
    assert res["outperformed"] is False


async def test_compare_with_benchmark_no_history(monkeypatch, fake_db):
    p = _portfolio(initial_balance=1000.0)
    fake_db.queue_fetchone((p.model_dump(),))

    async def _hist(pf_id, user, period="1mo"):
        return []

    monkeypatch.setattr(portfolio_module, "get_portfolio_history", _hist)
    assert await portfolio_module.compare_with_benchmark("port-test", 1) is None


async def test_analyze_portfolio_performance(monkeypatch, fake_db):
    p = _portfolio()
    d1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    d2 = datetime(2026, 8, 10, tzinfo=timezone.utc)
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0, date=d1))
    fake_db.queue_fetchone((p.model_dump(),))

    async def _hist(ticker, start=None, end=None):
        return [
            {"ts": "2026-08-01T00:00:00+00:00", "price": 90.0},
            {"ts": "2026-08-10T00:00:00+00:00", "price": 110.0},
        ]

    monkeypatch.setattr(portfolio_module, "get_price_history", _hist)
    res = await portfolio_module.analyze_portfolio_performance("port-test", 1)
    assert res["overall"]["actual_pnl"] == pytest.approx(-1000.0)
    assert res["overall"]["optimal_pnl"] == pytest.approx(-900.0)
    assert res["overall"]["efficiency_score"] == 0.0
    a = res["assets"][0]
    assert a["ticker"] == "THYAO"
    assert a["optimal_points"]["best_buy"]["price"] == 90.0
    assert a["optimal_points"]["best_sell"]["price"] == 110.0


async def test_analyze_portfolio_performance_no_transactions(fake_db):
    p = _portfolio()
    fake_db.queue_fetchone((p.model_dump(),))
    res = await portfolio_module.analyze_portfolio_performance("port-test", 1)
    assert res == {"overall": None, "assets": []}


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


async def test_get_portfolio_snapshot(fake_db):
    p = _portfolio(initial_balance=2000.0, balance=1000.0)
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))
    fake_db.queue_fetchone(*((p.model_dump(),) for _ in range(6)))
    snap = await portfolio_module.get_portfolio_snapshot("port-test", 1)
    assert snap["portfolio"]["id"] == "port-test"
    assert snap["valuation"]["total_value"] == pytest.approx(2000.0)
    assert snap["transaction_stats"]["total_transactions"] == 1
    assert len(snap["recent_transactions"]) == 1
    assert snap["diversification"]["assets"][0]["type"] == "stock"


async def test_get_portfolio_snapshot_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.get_portfolio_snapshot("port-test", 1) is None


# ---------------------------------------------------------------------------
# CSV export / import
# ---------------------------------------------------------------------------


async def test_export_portfolio_csv(fake_db):
    p = _portfolio()
    p.transactions.append(_tx("THYAO", "BUY", 10, 100.0, commission=1.0))
    fake_db.queue_fetchone((p.model_dump(),))
    csv_out = await portfolio_module.export_portfolio_csv("port-test", 1)
    assert "date,ticker,type,quantity,price,total" in csv_out
    assert "THYAO,BUY,10.0,100.0,1000.0" in csv_out


async def test_export_portfolio_csv_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    assert await portfolio_module.export_portfolio_csv("port-test", 1) is None


async def test_import_transactions_csv_success(fake_db):
    p = _portfolio(balance=10000.0)
    fake_db.queue_fetchone((p.model_dump(),))
    content = (
        "ticker,type,quantity,price,date\n"
        "THYAO,BUY,10,100,2026-08-01T00:00:00+00:00\n"
        "THYAO,SELL,2,120,2026-08-02T00:00:00+00:00\n"
    )
    res = await portfolio_module.import_transactions_csv("port-test", 1, content)
    assert res["success"] is True
    assert res["imported"] == 2
    assert res["failed"] == 0
    assert len(_saved_portfolio(fake_db).transactions) == 2


async def test_import_transactions_csv_invalid_rows(fake_db):
    p = _portfolio(balance=10000.0)
    fake_db.queue_fetchone((p.model_dump(),))
    content = (
        "ticker,type,quantity,price,date\n"
        ",BUY,10,100,2026-08-01T00:00:00+00:00\n"
        "THYAO,HOLD,10,100,2026-08-01T00:00:00+00:00\n"
        "THYAO,BUY,-1,100,2026-08-01T00:00:00+00:00\n"
        "THYAO,BUY,10,0,2026-08-01T00:00:00+00:00\n"
    )
    res = await portfolio_module.import_transactions_csv("port-test", 1, content)
    assert res["success"] is False
    assert res["imported"] == 0
    assert res["failed"] == 4
    assert len(res["errors"]) == 4


async def test_import_transactions_csv_not_found(fake_db):
    fake_db.queue_fetchone((None,))
    res = await portfolio_module.import_transactions_csv("port-test", 1, "x")
    assert res["success"] is False
    assert "Portfolio not found" in res["message"]


async def test_import_transactions_csv_save_failure(monkeypatch, fake_db):
    p = _portfolio(balance=10000.0)
    fake_db.queue_fetchone((p.model_dump(),))

    async def _fail(pf):
        return False

    monkeypatch.setattr(portfolio_module, "save_portfolio", _fail)
    content = "ticker,type,quantity,price,date\nTHYAO,BUY,10,100,2026-08-01T00:00:00+00:00\n"
    res = await portfolio_module.import_transactions_csv("port-test", 1, content)
    assert res["success"] is False
    assert "Could not save" in res["message"]
    assert res["imported"] == 0