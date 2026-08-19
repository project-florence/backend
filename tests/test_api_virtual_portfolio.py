"""Unit tests for src/api/virtual_portfolio.py.

The router is a thin wrapper over ``src.services.portfolio``; the service
module is stubbed per-test so no real DB, Redis or network access happens.
Success paths and 4xx error paths (bad payload / not found) are covered.
"""

from src.api.virtual_portfolio import router as vp_router
from src.services import portfolio as svc_module

from api_helpers import build_app, request


class _FakeModel:
    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


def _portfolio(name="Test Portföy", balance=10000.0):
    return _FakeModel(
        {
            "metadata": {
                "id": "pf-1",
                "user_id": 7,
                "name": name,
                "initial_balance": balance,
                "balance": balance,
                "created_at": "2026-08-20T09:00:00+00:00",
                "updated_at": "2026-08-20T09:00:00+00:00",
            },
            "transactions": [],
        }
    )


def _patch_svc(monkeypatch, **funcs):
    for name, func in funcs.items():
        monkeypatch.setattr(svc_module, name, func)
    return funcs


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_portfolio_success(monkeypatch, fake_db, fake_redis):
    async def _create(user_id, name, initial_balance):
        assert user_id == 7
        assert name == "Test Portföy"
        assert initial_balance == 10000.0
        return _portfolio(name=name, balance=initial_balance)

    _patch_svc(monkeypatch, create_portfolio=_create)

    app = build_app(vp_router)
    resp = await request(
        app, "POST", "/portfolios", json={"name": "Test Portföy", "initial_balance": 10000.0}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["id"] == "pf-1"
    assert body["metadata"]["name"] == "Test Portföy"


async def test_create_portfolio_bad_payload(monkeypatch, fake_db, fake_redis):
    _patch_svc(monkeypatch, create_portfolio=lambda *a, **k: _portfolio())

    app = build_app(vp_router)

    empty_name = await request(app, "POST", "/portfolios", json={"name": "", "initial_balance": 100})
    assert empty_name.status_code == 422

    zero_balance = await request(app, "POST", "/portfolios", json={"name": "X", "initial_balance": 0})
    assert zero_balance.status_code == 422

    negative = await request(app, "POST", "/portfolios", json={"name": "X", "initial_balance": -5})
    assert negative.status_code == 422


async def test_create_portfolio_service_failure(monkeypatch, fake_db, fake_redis):
    async def _create(user_id, name, initial_balance):
        return None

    _patch_svc(monkeypatch, create_portfolio=_create)

    app = build_app(vp_router)
    resp = await request(
        app, "POST", "/portfolios", json={"name": "X", "initial_balance": 100}
    )

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to create portfolio"


# ---------------------------------------------------------------------------
# list / get
# ---------------------------------------------------------------------------


async def test_list_portfolios(monkeypatch, fake_db, fake_redis):
    async def _list(user_id):
        return [_portfolio(name="A"), _portfolio(name="B")]

    _patch_svc(monkeypatch, list_portfolios=_list)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios")

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert items[0]["metadata"]["name"] == "A"
    assert items[1]["metadata"]["name"] == "B"


async def test_get_portfolio_success(monkeypatch, fake_db, fake_redis):
    async def _load(portfolio_id, user_id):
        assert portfolio_id == "pf-1"
        return _portfolio()

    _patch_svc(monkeypatch, load_portfolio=_load)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1")

    assert resp.status_code == 200
    assert resp.json()["metadata"]["id"] == "pf-1"


async def test_get_portfolio_not_found(monkeypatch, fake_db, fake_redis):
    async def _load(portfolio_id, user_id):
        return None

    _patch_svc(monkeypatch, load_portfolio=_load)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/nope")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Portfolio not found"


# ---------------------------------------------------------------------------
# rename / delete / duplicate
# ---------------------------------------------------------------------------


async def test_rename_portfolio_success(monkeypatch, fake_db, fake_redis):
    async def _rename(portfolio_id, user_id, name):
        return True

    _patch_svc(monkeypatch, rename_portfolio=_rename)

    app = build_app(vp_router)
    resp = await request(app, "PUT", "/portfolios/pf-1", json={"name": "Yeni"})

    assert resp.status_code == 200
    assert resp.json() == {"message": "Portfolio renamed"}


async def test_rename_portfolio_not_found(monkeypatch, fake_db, fake_redis):
    async def _rename(portfolio_id, user_id, name):
        return False

    _patch_svc(monkeypatch, rename_portfolio=_rename)

    app = build_app(vp_router)
    resp = await request(app, "PUT", "/portfolios/nope", json={"name": "Yeni"})

    assert resp.status_code == 404


async def test_delete_portfolio_success(monkeypatch, fake_db, fake_redis):
    async def _delete(portfolio_id, user_id):
        return True

    _patch_svc(monkeypatch, delete_portfolio=_delete)

    app = build_app(vp_router)
    resp = await request(app, "DELETE", "/portfolios/pf-1")

    assert resp.status_code == 200
    assert resp.json() == {"message": "Portfolio deleted"}


async def test_delete_portfolio_not_found(monkeypatch, fake_db, fake_redis):
    async def _delete(portfolio_id, user_id):
        return False

    _patch_svc(monkeypatch, delete_portfolio=_delete)

    app = build_app(vp_router)
    resp = await request(app, "DELETE", "/portfolios/nope")

    assert resp.status_code == 404


async def test_duplicate_portfolio_success(monkeypatch, fake_db, fake_redis):
    async def _duplicate(portfolio_id, user_id, name):
        return _portfolio(name=name)

    _patch_svc(monkeypatch, duplicate_portfolio=_duplicate)

    app = build_app(vp_router)
    resp = await request(app, "POST", "/portfolios/pf-1/duplicate", json={"name": "Kopya"})

    assert resp.status_code == 200
    assert resp.json()["metadata"]["name"] == "Kopya"


async def test_duplicate_portfolio_not_found(monkeypatch, fake_db, fake_redis):
    async def _duplicate(portfolio_id, user_id, name):
        return None

    _patch_svc(monkeypatch, duplicate_portfolio=_duplicate)

    app = build_app(vp_router)
    resp = await request(app, "POST", "/portfolios/nope/duplicate", json={"name": "Kopya"})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------


async def test_get_transactions_success(monkeypatch, fake_db, fake_redis):
    async def _txs(portfolio_id, user_id, ticker=None, tx_type=None, start=None, end=None):
        assert portfolio_id == "pf-1"
        assert ticker == "THYAO"
        return [
            _FakeModel(
                {"id": "t1", "ticker": "THYAO", "type": "BUY", "quantity": 10.0, "price": 100.0}
            )
        ]

    _patch_svc(monkeypatch, get_transactions=_txs)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/transactions?ticker=THYAO")

    assert resp.status_code == 200
    assert resp.json()[0]["ticker"] == "THYAO"


async def test_get_transactions_not_found(monkeypatch, fake_db, fake_redis):
    async def _txs(portfolio_id, user_id, **kwargs):
        return None

    _patch_svc(monkeypatch, get_transactions=_txs)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/nope/transactions")

    assert resp.status_code == 404


async def test_add_transaction_success(monkeypatch, fake_db, fake_redis):
    async def _add(portfolio_id, user_id, ticker, tx_type, quantity):
        return True

    _patch_svc(monkeypatch, add_transaction=_add)

    app = build_app(vp_router)
    resp = await request(
        app,
        "POST",
        "/portfolios/pf-1/transactions",
        json={"ticker": "THYAO", "type": "BUY", "quantity": 10},
    )

    assert resp.status_code == 200
    assert resp.json() == {"message": "Transaction added"}


async def test_add_transaction_failure(monkeypatch, fake_db, fake_redis):
    async def _add(portfolio_id, user_id, ticker, tx_type, quantity):
        return False

    _patch_svc(monkeypatch, add_transaction=_add)

    app = build_app(vp_router)
    resp = await request(
        app,
        "POST",
        "/portfolios/pf-1/transactions",
        json={"ticker": "THYAO", "type": "BUY", "quantity": 10},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Transaction failed"


async def test_add_transaction_bad_payload(monkeypatch, fake_db, fake_redis):
    _patch_svc(monkeypatch, add_transaction=lambda *a, **k: True)

    app = build_app(vp_router)

    bad_type = await request(
        app,
        "POST",
        "/portfolios/pf-1/transactions",
        json={"ticker": "THYAO", "type": "HODL", "quantity": 10},
    )
    assert bad_type.status_code == 422

    bad_qty = await request(
        app,
        "POST",
        "/portfolios/pf-1/transactions",
        json={"ticker": "THYAO", "type": "BUY", "quantity": 0},
    )
    assert bad_qty.status_code == 422


async def test_update_transaction_success(monkeypatch, fake_db, fake_redis):
    async def _update(portfolio_id, user_id, tx_id, price=None, quantity=None):
        return True

    _patch_svc(monkeypatch, update_transaction=_update)

    app = build_app(vp_router)
    resp = await request(
        app, "PUT", "/portfolios/pf-1/transactions/t1", json={"price": 150.0}
    )

    assert resp.status_code == 200
    assert resp.json() == {"message": "Transaction updated"}


async def test_update_transaction_not_found(monkeypatch, fake_db, fake_redis):
    async def _update(portfolio_id, user_id, tx_id, price=None, quantity=None):
        return False

    _patch_svc(monkeypatch, update_transaction=_update)

    app = build_app(vp_router)
    resp = await request(
        app, "PUT", "/portfolios/pf-1/transactions/t1", json={"price": 150.0}
    )

    assert resp.status_code == 404


async def test_undo_last_transaction_success(monkeypatch, fake_db, fake_redis):
    async def _undo(portfolio_id, user_id):
        return True

    _patch_svc(monkeypatch, undo_last_transaction=_undo)

    app = build_app(vp_router)
    resp = await request(app, "DELETE", "/portfolios/pf-1/transactions/undo")

    assert resp.status_code == 200
    assert resp.json() == {"message": "Last transaction undone"}


async def test_undo_last_transaction_nothing(monkeypatch, fake_db, fake_redis):
    async def _undo(portfolio_id, user_id):
        return False

    _patch_svc(monkeypatch, undo_last_transaction=_undo)

    app = build_app(vp_router)
    resp = await request(app, "DELETE", "/portfolios/pf-1/transactions/undo")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Nothing to undo"


# ---------------------------------------------------------------------------
# analytics endpoints
# ---------------------------------------------------------------------------


async def test_portfolio_valuation_success(monkeypatch, fake_db, fake_redis):
    async def _val(portfolio_id, user_id):
        return {"total_value": 15000.0}

    _patch_svc(monkeypatch, get_portfolio_valuation=_val)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/valuation")

    assert resp.status_code == 200
    assert resp.json() == {"total_value": 15000.0}


async def test_portfolio_valuation_not_found(monkeypatch, fake_db, fake_redis):
    async def _val(portfolio_id, user_id):
        return None

    _patch_svc(monkeypatch, get_portfolio_valuation=_val)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/nope/valuation")

    assert resp.status_code == 404


async def test_portfolio_diversification_success(monkeypatch, fake_db, fake_redis):
    async def _div(portfolio_id, user_id):
        return {"sectors": {}}

    _patch_svc(monkeypatch, get_diversification=_div)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/diversification")

    assert resp.status_code == 200
    assert resp.json() == {"sectors": {}}


async def test_portfolio_performers_success(monkeypatch, fake_db, fake_redis):
    async def _perf(portfolio_id, user_id, top_n=5):
        assert top_n == 3
        return {"best": [], "worst": []}

    _patch_svc(monkeypatch, get_best_worst_performers=_perf)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/performers?top_n=3")

    assert resp.status_code == 200
    assert resp.json() == {"best": [], "worst": []}


async def test_portfolio_history_success(monkeypatch, fake_db, fake_redis):
    async def _hist(portfolio_id, user_id, period="1mo"):
        assert period == "3mo"
        return {"points": []}

    _patch_svc(monkeypatch, get_portfolio_history=_hist)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/history?period=3mo")

    assert resp.status_code == 200
    assert resp.json() == {"points": []}


async def test_portfolio_history_bad_period(monkeypatch, fake_db, fake_redis):
    _patch_svc(monkeypatch, get_portfolio_history=lambda *a, **k: {"points": []})

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/history?period=decade")

    assert resp.status_code == 422


async def test_portfolio_returns_success(monkeypatch, fake_db, fake_redis):
    async def _returns(portfolio_id, user_id, period="1mo"):
        return {"returns": 0.12}

    _patch_svc(monkeypatch, get_returns=_returns)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/returns")

    assert resp.status_code == 200
    assert resp.json() == {"returns": 0.12}


async def test_portfolio_risk_success(monkeypatch, fake_db, fake_redis):
    async def _risk(portfolio_id, user_id, period="1y"):
        return {"risk": 0.3}

    _patch_svc(monkeypatch, get_risk_metrics=_risk)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/risk")

    assert resp.status_code == 200
    assert resp.json() == {"risk": 0.3}


async def test_portfolio_benchmark_success(monkeypatch, fake_db, fake_redis):
    async def _bench(portfolio_id, user_id, benchmark_ticker="XU100"):
        assert benchmark_ticker == "XU030"
        return {"alpha": 0.05}

    _patch_svc(monkeypatch, compare_with_benchmark=_bench)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/benchmark?ticker=XU030")

    assert resp.status_code == 200
    assert resp.json() == {"alpha": 0.05}


async def test_portfolio_benchmark_empty(monkeypatch, fake_db, fake_redis):
    async def _bench(portfolio_id, user_id, benchmark_ticker="XU100"):
        return None

    _patch_svc(monkeypatch, compare_with_benchmark=_bench)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/benchmark")

    assert resp.status_code == 200
    assert resp.json() == {}


async def test_portfolio_performance_success(monkeypatch, fake_db, fake_redis):
    async def _perf(portfolio_id, user_id):
        return {"performance": 1.1}

    _patch_svc(monkeypatch, analyze_portfolio_performance=_perf)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/performance")

    assert resp.status_code == 200
    assert resp.json() == {"performance": 1.1}


async def test_portfolio_stats_success(monkeypatch, fake_db, fake_redis):
    async def _stats(portfolio_id, user_id):
        return {"count": 5}

    _patch_svc(monkeypatch, get_transaction_stats=_stats)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/stats")

    assert resp.status_code == 200
    assert resp.json() == {"count": 5}


async def test_portfolio_snapshot_success(monkeypatch, fake_db, fake_redis):
    async def _snap(portfolio_id, user_id):
        return {"snapshot": True}

    _patch_svc(monkeypatch, get_portfolio_snapshot=_snap)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/snapshot")

    assert resp.status_code == 200
    assert resp.json() == {"snapshot": True}


async def test_portfolio_export_csv_success(monkeypatch, fake_db, fake_redis):
    async def _csv(portfolio_id, user_id):
        return "ticker,qty\nTHYAO,10\n"

    _patch_svc(monkeypatch, export_portfolio_csv=_csv)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/pf-1/export/csv")

    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert resp.text == "ticker,qty\nTHYAO,10\n"


async def test_portfolio_export_csv_not_found(monkeypatch, fake_db, fake_redis):
    async def _csv(portfolio_id, user_id):
        return None

    _patch_svc(monkeypatch, export_portfolio_csv=_csv)

    app = build_app(vp_router)
    resp = await request(app, "GET", "/portfolios/nope/export/csv")

    assert resp.status_code == 404