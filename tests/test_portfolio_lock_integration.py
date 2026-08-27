"""Gercek Postgres'e karsi calisan, opt-in entegrasyon testi.

TEST_COVERAGE_PLAN.md Adim B'nin en kritik maddesi: ``BEKLEYENLER.md``'nin
en yuksek oncelikli acik riski, ``src/services/portfolio.py::_lock_portfolio``'nun
``keep=True`` advisory-lock baglantisinin, kilit ile kayit (save) arasinda
calisan bir ``keep=False`` DB cagrisi tarafindan erken serbest birakilmasi
("kilit islem ortasinda dusuyor"). Bu, tam olarak gercek Postgres'in
transaction-scoped ``pg_advisory_xact_lock``'unu ve gercek baglanti
havuzunu gerektirir -- ``fake_db`` bu sinifi hatayi yapisal olarak goremez
(bkz. tests/test_core_database.py -- ayni mekanizmanin hermetik/mock'lu
kanitini icerir, gercek kilit/blok davranisi olmadan).

Bu dosyadaki iki test birbirini tamamlar:

1. ``test_lock_mechanism_allows_lost_update_when_keep_false_query_runs_between_lock_and_save``
   -- mekanizma hala KIRIK: kilit ile kayit arasina bilerek bir
   ``keep=False`` sorgu sokulursa (``import_transactions_csv``'nin
   ``is_valid_ticker`` dongu-ici cagrisiyla ayni kalip), iki eszamanli
   yazim birbirinin islemini kaybeder (lost update). BU DAVRANIS YANLIS;
   bilerek DUZELTILMEDI (bkz. asagidaki docstring ve rapor) -- kirmizi
   birakmiyoruz, mevcut (kotu) davranisi belgeliyoruz.

2. ``test_add_transaction_concurrent_buys_are_serialized_by_the_lock``
   -- FIX REGRESYONU: ``add_transaction`` bu adimda fiyati kilitten ONCE
   cozecek sekilde degistirildi (``src/services/portfolio.py``), boylece
   kilit -> load -> save arasinda artik ``keep=False`` bir cagri kalmiyor.
   Iki eszamanli BUY, gercek advisory lock ile serialize edilir; HICBIR
   islem kaybolmaz.

Calistirma
----------
    cd backend && source .venv/bin/activate
    docker compose up -d postgres redis
    python -m pytest -m integration -q tests/test_portfolio_lock_integration.py

Container'lar kapaliysa modul temiz sekilde skip edilir (asagidaki
``_integration_target`` probu, ``tests/test_llm_integration.py`` ile ayni
desen). Prod'a asla baglanilmaz (``_guard_not_local``, ayni desen).
"""

import asyncio
import os
import uuid

import psycopg
import pytest

import src.services.portfolio as svc
from src.core import database as database_module

pytestmark = pytest.mark.integration

_CONNECT_TIMEOUT = 2.0


def _guard_not_local() -> str | None:
    if os.getenv("FLORENCE_INTEGRATION_ALLOW_REMOTE") == "1":
        return None
    for var in ("POSTGRES_HOST", "REDIS_HOST"):
        host = os.getenv(var, "")
        if host not in ("localhost", "127.0.0.1", ""):
            return (
                f"{var}={host!r} yerel gorunmuyor; entegrasyon testleri prod'a "
                "asla baglanmamali (guvenlik agi). Kasitliyse "
                "FLORENCE_INTEGRATION_ALLOW_REMOTE=1 ile gecersiz kil."
            )
    return None


@pytest.fixture(scope="module")
def _integration_target() -> None:
    guard = _guard_not_local()
    if guard:
        pytest.skip(guard)

    async def _probe() -> str | None:
        try:
            conn = await psycopg.AsyncConnection.connect(
                database_module._conninfo(), connect_timeout=_CONNECT_TIMEOUT
            )
            await conn.close()
        except Exception as exc:
            return f"Postgres'e baglanilamadi (POSTGRES_PORT={os.getenv('POSTGRES_PORT')}): {exc}"
        return None

    failure = asyncio.run(_probe())
    if failure:
        pytest.skip(f"entegrasyon container'lari ayakta degil, atlaniyor -- {failure}")


@pytest.fixture(autouse=True)
async def _reset_singletons(_integration_target):
    """Her testte taze havuz -- pytest-asyncio her test icin yeni bir loop acar."""
    yield
    await database_module.db.close()


@pytest.fixture
async def _portfolio_ctx():
    """Gercek bir kullanici + bos portfoy olusturur, testten sonra temizler."""
    await database_module.init_db()

    username = f"itest-lock-{uuid.uuid4().hex[:10]}"
    async with database_module.db.cursor(row_factory=None) as cur:
        await cur.execute(
            "INSERT INTO users (username, email, hashed_pw) VALUES (%s, %s, %s) RETURNING id",
            (username, f"{username}@example.test", "hash:x"),
        )
        user_id = (await cur.fetchone())[0]
        await database_module.db.commit()

    portfolio = await svc.create_portfolio(user_id, "Lock Test", 100_000.0)
    assert portfolio is not None

    yield portfolio.metadata.id, user_id

    async with database_module.db.cursor(row_factory=None) as cur:
        await cur.execute("DELETE FROM portfolios WHERE user_id = %s", (user_id,))
        await cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        await database_module.db.commit()


async def _true_async(*_a, **_k):
    return True


# ---------------------------------------------------------------------------
# 1) Mekanizma hala kirik: kilit + arada keep=False sorgu -> lost update
# ---------------------------------------------------------------------------


async def test_lock_mechanism_allows_lost_update_when_keep_false_query_runs_between_lock_and_save(
    _portfolio_ctx,
):
    """``import_transactions_csv``'nin dongu ici ``is_valid_ticker`` cagrisiyla
    (ve fix ONCESI ``add_transaction``'in ``get_current_price`` cagrisiyla)
    ayni kalibi dogrudan simule eder: ``_lock_portfolio`` -> ARADA bir
    ``keep=False`` DB sorgusu -> ``load_portfolio`` -> mutasyon -> ``save_portfolio``.

    Iki eszamanli cagri, senkronizasyon noktalariyla, ikisi de "load"
    asamasini bitirene KADAR "save" asamasina gecmemeye zorlanir -- boylece
    zamanlama sansa birakilmaz, kaybin GERCEKTEN o araya sikisan keep=False
    sorgudan kaynaklandigi kanitlanir (kilit dogru calissaydi ikinci cagrinin
    ``_lock_portfolio``'su birincinin ``save``'i bitene kadar GERCEKTEN
    Postgres seviyesinde bloke olurdu).

    BU TEST GECER ve mevcut (yanlis) davranisi belgeler: iki islemden biri
    kaybolur. Bilerek duzeltilmedi -- bkz. dosya docstring'i ve rapor.
    """
    portfolio_id, user_id = _portfolio_ctx

    loaded_a = asyncio.Event()
    loaded_b = asyncio.Event()
    proceed_save = asyncio.Event()

    async def _flow(tx_marker: str, loaded_event: asyncio.Event):
        await svc._lock_portfolio(portfolio_id)

        # ARADAKI keep=False cagri: is_valid_ticker'in DB'ye dustugu an gibi
        # (bkz. src/services/bist.py::get_bist_tickers_as_dict_from_redis ->
        # _db_get_tickers, redis miss'inde). Varsayilan ``db.cursor()``
        # (keep=False) -- bu blok cikisinda kilit baglantisi rollback edilip
        # havuza iade edilir.
        async with database_module.db.cursor(row_factory=None) as cur:
            await cur.execute("SELECT 1")

        portfolio = await svc.load_portfolio(portfolio_id, user_id)
        loaded_event.set()
        await proceed_save.wait()

        portfolio.transactions.append(
            svc.Transaction(
                id=tx_marker, ticker="THYAO", type="BUY", quantity=1.0, price=100.0,
                commission=0.0, total=100.0,
                date=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
        )
        await svc.save_portfolio(portfolio)

    async def _releaser():
        await loaded_a.wait()
        await loaded_b.wait()
        proceed_save.set()

    await asyncio.gather(
        _flow("tx-A", loaded_a),
        _flow("tx-B", loaded_b),
        _releaser(),
    )

    final = await svc.load_portfolio(portfolio_id, user_id)
    tx_ids = {tx.id for tx in final.transactions}

    # BEKLENEN (dogru) davranis iki islem de kalicidir: {"tx-A", "tx-B"}.
    # GERCEKLESEN: kilit erken dustugu icin ikinci ``_lock_portfolio`` birinciyi
    # BEKLEMEDEN gecti, iki cagri da AYNI (islemsiz) portfoyu yukledi ve
    # sonuncusu kaydeden digerinin yazdigini sessizce ezdi.
    assert len(tx_ids) == 1, (
        f"beklenen: lost update (1 islem kaldi), gozlemlenen tx id'ler: {tx_ids} -- "
        "eger bu assert 2 donuyorsa mekanizma duzelmis olabilir, rapor guncellenmeli"
    )
    assert tx_ids <= {"tx-A", "tx-B"}


# ---------------------------------------------------------------------------
# 2) FIX regresyonu: add_transaction artik fiyati kilitten ONCE cozuyor
# ---------------------------------------------------------------------------


async def test_add_transaction_concurrent_buys_are_serialized_by_the_lock(
    _portfolio_ctx, monkeypatch
):
    """Bu adimda uygulanan duzeltmenin regresyon testi (bkz.
    ``src/services/portfolio.py::add_transaction`` -- fiyat artik kilitten
    ONCE cozuluyor). Kilit -> load -> save arasinda artik ``keep=False`` bir
    DB cagrisi YOK; iki eszamanli BUY gercek Postgres advisory kilidiyle
    dogru sekilde serialize edilmeli ve HICBIRI kaybolmamali -- yukaridaki
    testle (ayni kalip ama arada bir keep=False cagri VAR) tam bir zit
    ornek olusturur.
    """
    portfolio_id, user_id = _portfolio_ctx

    monkeypatch.setattr(svc, "get_market_status", lambda: "open")
    monkeypatch.setattr(svc, "is_valid_ticker", _true_async)

    async def _fixed_price(ticker, interval="5m"):
        return 100.0

    monkeypatch.setattr(svc, "get_current_price", _fixed_price)

    ok_a, ok_b = await asyncio.gather(
        svc.add_transaction(portfolio_id, user_id, "THYAO", "BUY", 5),
        svc.add_transaction(portfolio_id, user_id, "THYAO", "BUY", 3),
    )

    assert ok_a is True
    assert ok_b is True

    final = await svc.load_portfolio(portfolio_id, user_id)
    assert len(final.transactions) == 2, (
        f"iki eszamanli add_transaction'dan biri kayboldu: {len(final.transactions)} "
        "islem kaldi (2 bekleniyordu) -- kilit fix'i regresyona ugramis olabilir"
    )
    quantities = sorted(tx.quantity for tx in final.transactions)
    assert quantities == [3.0, 5.0]

    # Bakiye HER IKI islemin de komisyonlu maliyetini yansitmali.
    rate = svc._commission_rate()
    expected_cost = (5 * 100.0 * (1 + rate)) + (3 * 100.0 * (1 + rate))
    assert final.metadata.balance == pytest.approx(100_000.0 - expected_cost, abs=0.01)
