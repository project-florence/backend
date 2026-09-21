"""IDOR (Insecure Direct Object Reference) regresyon testleri.

``web/REVIEW_REPORT.md``'de anilan "portfoy IDOR" maddesi -- dosya artik yok
ama risk gercek: ``GET/PUT/DELETE /portfolios/{portfolio_id}`` ailesi,
sahiplik kontrolunu SADECE ``src.services.portfolio.load_portfolio`` (ve
kardesleri) icindeki SQL WHERE kosuluna (``portfolio_id = %s AND user_id =
%s``) borclu -- ``user_id`` HER ZAMAN ``Depends(get_current_user)``'dan
gelir, istek govdesinden/path'ten asla alinmaz.

``tests/test_api_virtual_portfolio.py`` bu router'i zaten kapsamli test
ediyor AMA HER TESTTE ``src.services.portfolio`` fonksiyonlari stub'lanip
gecistiriliyor -- bu, IDOR'u YAKALAYAMAZ: router "yanlis" bir kullaniciyi
servis katmanina iletse bile stub bunu umursamaz. Bu dosya bilerek servis
katmanini STUB'LAMAZ; gercek ``svc.load_portfolio`` / ``rename_portfolio`` /
``delete_portfolio`` calisir, ``fake_db`` uzerinden SQL'e giden PARAMETRELERI
dogrudan denetler -- boylece hem "router dogru user_id'yi iletiyor mu" hem
"servis bunu WHERE'e koyuyor mu" tek testte kanitlanir.
"""

from datetime import datetime, timezone

import src.services.portfolio as portfolio_module
from src.api.virtual_portfolio import router as vp_router
from src.services.portfolio import Metadata, Portfolio

from api_helpers import build_app, request

OWNER_ID = 7
ATTACKER_ID = 999
PORTFOLIO_ID = "port-victim"


def _owner_portfolio() -> dict:
    now = datetime.now(timezone.utc)
    return Portfolio(
        metadata=Metadata(
            id=PORTFOLIO_ID,
            user_id=OWNER_ID,
            name="Victim Portfolio",
            initial_balance=10000.0,
            balance=10000.0,
            created_at=now,
            updated_at=now,
        ),
    ).model_dump()


# ---------------------------------------------------------------------------
# GET /portfolios/{id} -- read IDOR
# ---------------------------------------------------------------------------


async def test_get_portfolio_denies_non_owner_and_forwards_real_user_id(fake_db, fake_redis):
    # Gercek Postgres'in mismatched WHERE (portfolio_id = %s AND user_id = %s)
    # icin yapacagi seyi simule eder: satir donmez.
    fake_db.queue_fetchone((None,))

    app = build_app(vp_router, user_id=ATTACKER_ID)
    resp = await request(app, "GET", f"/portfolios/{PORTFOLIO_ID}")

    assert resp.status_code == 404
    selects = [q for q in fake_db.queries if "SELECT portfolio FROM portfolios" in q[0]]
    assert len(selects) == 1
    # Router, get_current_user'in cozdugu (saldirganin) id'sini SQL'e iletti --
    # istek govdesinde/path'te saldirganin kontrol ettigi hicbir alan yok.
    assert selects[0][1] == (PORTFOLIO_ID, ATTACKER_ID)


async def test_get_portfolio_succeeds_for_actual_owner(fake_db, fake_redis):
    fake_db.queue_fetchone((_owner_portfolio(),))

    app = build_app(vp_router, user_id=OWNER_ID)
    resp = await request(app, "GET", f"/portfolios/{PORTFOLIO_ID}")

    assert resp.status_code == 200
    assert resp.json()["metadata"]["id"] == PORTFOLIO_ID
    selects = [q for q in fake_db.queries if "SELECT portfolio FROM portfolios" in q[0]]
    assert selects[0][1] == (PORTFOLIO_ID, OWNER_ID)


# ---------------------------------------------------------------------------
# DELETE /portfolios/{id} -- yazma IDOR
# ---------------------------------------------------------------------------


async def test_delete_portfolio_denies_non_owner(fake_db, fake_redis):
    fake_db.rowcount = 0  # gercek DB: WHERE user_id = saldirgan eslesmedi -> 0 satir

    app = build_app(vp_router, user_id=ATTACKER_ID)
    resp = await request(app, "DELETE", f"/portfolios/{PORTFOLIO_ID}")

    assert resp.status_code == 404
    deletes = [q for q in fake_db.queries if q[0].strip().startswith("DELETE FROM portfolios")]
    assert deletes[0][1] == (PORTFOLIO_ID, ATTACKER_ID)


async def test_delete_portfolio_succeeds_for_actual_owner(fake_db, fake_redis):
    fake_db.rowcount = 1

    app = build_app(vp_router, user_id=OWNER_ID)
    resp = await request(app, "DELETE", f"/portfolios/{PORTFOLIO_ID}")

    assert resp.status_code == 200
    deletes = [q for q in fake_db.queries if q[0].strip().startswith("DELETE FROM portfolios")]
    assert deletes[0][1] == (PORTFOLIO_ID, OWNER_ID)


# ---------------------------------------------------------------------------
# PUT /portfolios/{id} (rename) -- yazma IDOR (load+save uzerinden dolayli)
# ---------------------------------------------------------------------------


async def test_rename_portfolio_denies_non_owner(fake_db, fake_redis):
    # rename_portfolio -> load_portfolio(portfolio_id, user_id) once cagirir;
    # saldirganin id'siyle satir bulunamaz -> False -> 404. save_portfolio'ya
    # (INSERT) hic ulasilmamali.
    fake_db.queue_fetchone((None,))

    app = build_app(vp_router, user_id=ATTACKER_ID)
    resp = await request(app, "PUT", f"/portfolios/{PORTFOLIO_ID}", json={"name": "Hacked"})

    assert resp.status_code == 404
    selects = [q for q in fake_db.queries if "SELECT portfolio FROM portfolios" in q[0]]
    assert selects[0][1] == (PORTFOLIO_ID, ATTACKER_ID)
    inserts = [q for q in fake_db.queries if "INSERT INTO portfolios" in q[0]]
    assert inserts == []  # saldirganin adi degistirilmedi


async def test_rename_portfolio_succeeds_for_actual_owner(fake_db, fake_redis):
    fake_db.queue_fetchone((_owner_portfolio(),))

    app = build_app(vp_router, user_id=OWNER_ID)
    resp = await request(app, "PUT", f"/portfolios/{PORTFOLIO_ID}", json={"name": "New Name"})

    assert resp.status_code == 200
    inserts = [q for q in fake_db.queries if "INSERT INTO portfolios" in q[0]]
    assert len(inserts) == 1
    saved = Portfolio.model_validate_json(inserts[0][1][2])
    assert saved.metadata.name == "New Name"
    assert saved.metadata.user_id == OWNER_ID


# ---------------------------------------------------------------------------
# GET /portfolios/{id}/transactions -- load_portfolio uzerinden dolayli IDOR
# ---------------------------------------------------------------------------


async def test_get_transactions_denies_non_owner(fake_db, fake_redis):
    fake_db.queue_fetchone((None,))

    app = build_app(vp_router, user_id=ATTACKER_ID)
    resp = await request(app, "GET", f"/portfolios/{PORTFOLIO_ID}/transactions")

    assert resp.status_code == 404
    selects = [q for q in fake_db.queries if "SELECT portfolio FROM portfolios" in q[0]]
    assert selects[0][1] == (PORTFOLIO_ID, ATTACKER_ID)


# ---------------------------------------------------------------------------
# POST /portfolios/{id}/transactions -- piyasa kapaliyken islem reddi
# (svc.add_transaction gercek -- stub'lanmadi -- HTTPException'in router
# katmanindan dogru sekilde 400 olarak gectigini dogrular)
# ---------------------------------------------------------------------------


async def test_add_transaction_rejected_when_market_closed(fake_db, fake_redis, monkeypatch):
    monkeypatch.setattr(portfolio_module, "get_market_status", lambda: "closed")

    app = build_app(vp_router, user_id=OWNER_ID)
    resp = await request(
        app,
        "POST",
        f"/portfolios/{PORTFOLIO_ID}/transactions",
        json={"ticker": "THYAO", "type": "BUY", "quantity": 10},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "error_market_closed"
    # Piyasa kapaliyken kilit/DB'ye hic ulasilmamali.
    assert fake_db.queries == []
