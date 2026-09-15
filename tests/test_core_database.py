"""Hermetik testler: ``src/core/database.py``'nin baglanti/kilit mekanizmasi.

Bu dosya ``fake_db`` (yuksek seviye ``db.cursor``/``commit``/... yamasi)
KULLANMAZ -- amaci tam tersi: ``_AsyncDatabase.cursor(keep=...)``'in GERCEK
uygulamasini, sadece en alttaki ``_get_pool()``'u sahte (hermetik) bir
havuzla degistirerek test etmek. Boylece ``TEST_COVERAGE_PLAN.md`` Adim
B'nin en kritik maddesi -- portfolio ``_lock_portfolio``'nun ``keep=True``
baglantisinin, aradaki bir ``keep=False`` cursor blogu tarafindan erken
serbest birakilmasi -- gercek Postgres'e ihtiyac duyulmadan, mekanizmanin
kendisi uzerinde dogrudan kanitlanir (bkz. tests/test_portfolio_lock_integration.py
icin gercek-Postgres/uygulama-seviyesi karsiligi).

``_forbid_real_db_and_redis_sockets`` (conftest.py, autouse) her testte
``database._get_pool``'u ``pytest.fail`` eden bir sahte ile degistirir; bu
dosyadaki testler KENDI ``_get_pool`` yamalarini test govdesi icinde
uygulayarak (ayni ``monkeypatch`` nesnesi uzerinden, guard'inkinden SONRA
cagrilir) o korumayi hermetik bir sahte havuzla gecersiz kilar -- gercek
sokete hic dokunulmaz.
"""

import asyncio

import pytest

from src.core import database as db_module


class FakeCursor:
    """``conn.cursor(...)`` -> async context manager; execute/fetchone kaydeder."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=None):
        self._conn.executed.append((query, params))

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []


class FakeConn:
    def __init__(self, name):
        self.name = name
        self.executed: list = []
        self.committed = False
        self.rolledback = False

    def cursor(self, row_factory=None):
        return FakeCursor(self)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolledback = True


class FakePool:
    """Hermetik, bellek ici sahte ``AsyncConnectionPool``: her ``getconn()`` yeni bir ``FakeConn`` doner."""

    def __init__(self):
        self.conns: list[FakeConn] = []
        self.returned: list[FakeConn] = []

    async def getconn(self):
        conn = FakeConn(f"conn-{len(self.conns)}")
        self.conns.append(conn)
        return conn

    async def putconn(self, conn):
        self.returned.append(conn)


def _install_fake_pool(monkeypatch) -> FakePool:
    """``_forbid_real_db_and_redis_sockets`` guard'inin patch'ini bu testte gecersiz kilar."""
    pool = FakePool()

    async def _get_pool():
        return pool

    monkeypatch.setattr(db_module, "_get_pool", _get_pool)
    # Task-basina ContextVar'i onceki testlerden temiz baslat.
    db_module._current_conn.set(None)
    return pool


# ---------------------------------------------------------------------------
# BUG: keep=False bir cursor, ayni is'in keep=True baglantisini erken dusurur
# ---------------------------------------------------------------------------


async def test_keep_false_cursor_releases_prior_keep_true_connection(monkeypatch):
    """``_lock_portfolio``'nun advisory-lock deseninin tam olarak dustugu yer.

    1) ``keep=True`` bir blok (``_lock_portfolio`` benzeri) baglantiyi acik
       tutar -- ``pg_advisory_xact_lock`` gercek Postgres'te bu islem
       commit/rollback edilene kadar canlidir.
    2) Ayni is icinde, ``keep=False`` (varsayilan) bir cursor blogu
       (``get_current_price``'in ``price.py``'deki ``async with db.cursor()``
       SELECT'i gibi) ContextVar uzerinden AYNI baglantiyi alir; blok
       cikisinda ``_release_conn()`` cagrilir -- bu ROLLBACK yapar ve
       baglantiyi havuza iade eder. Gercek Postgres'te bu, ``keep=True``
       blogun aldigi advisory kilidi ANINDA dusurur (rollback islem sonu).
    3) Sonraki bir ``keep=True`` blok (``save_portfolio`` benzeri) artik
       FARKLI bir baglanti alir -- kilit hicbir zaman kayit islemini
       korumuyor demektir; iki eszamanli ``add_transaction`` cagrisi bu
       pencerede birbirinin degisikligini kaybedebilir (lost update).
    """
    pool = _install_fake_pool(monkeypatch)
    db = db_module.db

    async with db.cursor(row_factory=None, keep=True) as cur:
        await cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", ("port-1",))
    locked_conn = pool.conns[-1]
    assert locked_conn not in pool.returned, "kilit baglantisi henuz havuza donmemis olmali"
    assert locked_conn.rolledback is False

    async with db.cursor() as cur:  # keep=False, varsayilan -- get_current_price gibi
        await cur.execute("SELECT close FROM price_candles WHERE ticker = %s", ("THYAO.IS",))
        await cur.fetchone()

    # BUG dogrulandi: "kilit" baglantisi rollback edilip havuza iade edildi --
    # gercek Postgres'te bu, kilit islem ortasinda dusmus demek.
    assert locked_conn.rolledback is True
    assert locked_conn in pool.returned

    async with db.cursor(row_factory=None, keep=True) as cur:
        await cur.execute("INSERT INTO portfolios ...", ())
        await db.commit()
    save_conn = pool.conns[-1]

    assert save_conn is not locked_conn, (
        "kilit ve nihai kayit AYNI baglantida degil -- advisory lock "
        "kaydi korumuyor (BEKLEYENLER.md'nin en kritik acik riski)"
    )


# ---------------------------------------------------------------------------
# Duzeltilmis add_transaction sirasi: aradaki DB cagrisi olmadan tum kilitli
# bolum AYNI baglantiyi kullanir ve kilit gercekten korur.
# ---------------------------------------------------------------------------


async def test_consecutive_keep_true_blocks_share_one_connection_until_commit(monkeypatch):
    """``src/services/portfolio.py::add_transaction`` bu testin yazilmasina
    neden olan bulgudan sonra fiyati kilitten ONCE cozecek sekilde
    degistirildi (bkz. o dosyadaki yorum). Sonuc: kilit -> load -> save
    arasinda ``keep=False`` bir cagri kalmiyor, ve bu test tam olarak o
    deseni -- ic ice ``keep=True`` bloklarin AYNI baglantiyi paylastigini
    ve kilidin commit'e kadar canli kaldigini -- dogrular.
    """
    pool = _install_fake_pool(monkeypatch)
    db = db_module.db

    async with db.cursor(row_factory=None, keep=True) as cur:
        await cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", ("port-1",))
    locked_conn = pool.conns[-1]

    async with db.cursor(row_factory=None, keep=True) as cur:
        await cur.execute("SELECT portfolio FROM portfolios WHERE portfolio_id = %s", ("port-1",))
        await cur.fetchone()
    load_conn = pool.conns[-1]
    assert load_conn is locked_conn, "load, kilidi tutan AYNI baglantiyi kullanmali"
    assert locked_conn not in pool.returned, "kilit hala acik olmali"

    async with db.cursor(row_factory=None, keep=True) as cur:
        await cur.execute("INSERT INTO portfolios ...", ())
        await db.commit()
    save_conn = pool.conns[-1]

    assert save_conn is locked_conn, "kayit da AYNI (kilitli) baglantida yapilmali"
    assert locked_conn.committed is True
    assert locked_conn in pool.returned  # commit() -> _release_conn()


async def test_cursor_without_keep_releases_connection_on_normal_exit(monkeypatch):
    """Temel davranis: ``keep`` verilmezse (varsayilan False) blok cikisinda
    baglanti daima havuza doner -- SELECT-only / erken donen yollarda
    baglanti sizintisi olmamasini garanti eden mekanizma bu."""
    pool = _install_fake_pool(monkeypatch)
    db = db_module.db

    async with db.cursor() as cur:
        await cur.execute("SELECT 1")

    conn = pool.conns[-1]
    assert conn in pool.returned
    assert conn.rolledback is True  # commit edilmemis islem -> guvenli rollback


async def test_cursor_keep_true_survives_exception_but_release_current_cleans_up(monkeypatch):
    """``keep=True`` blok icinde exception firlarsa bile baglanti otomatik
    iade EDILMEZ (bilerek) -- cagiran taraf ``db.release_current()`` veya
    ``commit``/``rollback`` ile temizlemekle yukumlu (main.py middleware'i
    bunu her istek sonunda garanti eder, bkz. CLAUDE.md 'Auth' bolumu)."""
    pool = _install_fake_pool(monkeypatch)
    db = db_module.db

    try:
        async with db.cursor(row_factory=None, keep=True) as cur:
            await cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", ("port-2",))
            raise RuntimeError("islem ortasinda beklenmedik hata")
    except RuntimeError:
        pass

    leaked_conn = pool.conns[-1]
    assert leaked_conn not in pool.returned  # keep=True: otomatik iade YOK

    await db.release_current()
    assert leaked_conn in pool.returned  # middleware'in yaptigi temizlik


# ---------------------------------------------------------------------------
# Iptal-guvenli havuz alimi: CancelledError slot sizdirmamali (2026-09-15)
# ---------------------------------------------------------------------------


class BlockingPool(FakePool):
    """``getconn()`` disaridan serbest birakilana kadar bekler.

    Boylece iptal, havuz beklemesi SIRASINDA deterministik olarak
    uretilebilir: once task beklemeye girer, sonra disaridan ``cancel()``
    edilir, en son kapi acilip havuzun gec de olsa baglanti vermesi
    saglanir.
    """

    def __init__(self):
        super().__init__()
        self._gate = asyncio.Event()

    async def getconn(self):
        await self._gate.wait()
        return await super().getconn()

    def release_gate(self):
        self._gate.set()


class BlockingRollbackConn(FakeConn):
    """``rollback()`` kapi acilana kadar bekler (iade-ortasi iptal testi)."""

    def __init__(self, name):
        super().__init__(name)
        self._gate = asyncio.Event()
        self.entered_rollback = False

    async def rollback(self):
        self.entered_rollback = True
        await self._gate.wait()
        await super().rollback()

    def release_gate(self):
        self._gate.set()


class GatedPool(FakePool):
    """Tek seferlik, kapi kontrollu baglanti verir (iade-ortasi iptal testi)."""

    def __init__(self, conn):
        super().__init__()
        self._conn = conn

    async def getconn(self):
        return self._conn


def _install_custom_pool(monkeypatch, pool) -> None:
    async def _get_pool():
        return pool

    monkeypatch.setattr(db_module, "_get_pool", _get_pool)
    db_module._current_conn.set(None)


async def test_cancelled_checkout_returns_late_granted_connection(monkeypatch):
    """Bekleme sirasinda iptal + gec verilen baglanti cope gitmemeli.

    2026-09-15 503 dalgasinin kalici kismi: SPA sayfa gecisleri istek
    task'ini iptal ediyordu; korunmasiz ``await pool.getconn()`` sonrasi
    verilen baglanti sahipsiz kalip slot tuketiyordu (Postgres'te
    baglanti gorunmedigi icin teshis de zordu). Beklenti: iptal yayilir,
    ContextVar temiz kalir, gec verilen baglanti havuza iade edilir.
    """
    pool = BlockingPool()
    _install_custom_pool(monkeypatch, pool)

    task = asyncio.ensure_future(db_module._get_conn())
    await asyncio.sleep(0)  # task havuz beklemesine girsin
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # ContextVar kirlenmemis olmali (sahipsiz baglanti yok).
    assert db_module._current_conn.get() is None

    pool.release_gate()  # havuz simdi baglantiyi veriyor (kimse beklemiyor)
    await asyncio.sleep(0.05)  # bekci callback + iade task'i calissin

    assert len(pool.conns) == 1
    assert pool.conns[0] in pool.returned, (
        "gec verilen baglanti havuza iade edilmemis -- slot sizintisi"
    )
    assert db_module._current_conn.get() is None


async def test_cancelled_release_still_returns_connection(monkeypatch):
    """Iade ortasinda (rollback beklerken) iptal gelse bile slot donmeli.

    Beklenti: rollback tamamlanir, baglanti havuza iade edilir, iptal
    SONDA yeniden yukseltilir (gorev hijyeni: iptal yutulmaz).
    """
    conn = BlockingRollbackConn("conn-0")
    pool = GatedPool(conn)
    _install_custom_pool(monkeypatch, pool)

    async def _checkout_and_release():
        checkout = await db_module._get_conn()
        assert checkout is conn
        await db_module._release_conn()

    # Alim + iade AYNI task'ta olmali (ContextVar task-yereldir).
    task = asyncio.ensure_future(_checkout_and_release())
    for _ in range(100):
        if conn.entered_rollback:
            break
        await asyncio.sleep(0.01)
    assert conn.entered_rollback, "worker rollback beklemesine giremedi"
    assert not task.done()
    task.cancel()
    conn.release_gate()  # rollback tamamlanabilsin
    with pytest.raises(asyncio.CancelledError):
        await task
    # shield ile korunan rollback arka planda tamamlanir; birkac tick ver.
    for _ in range(100):
        if conn.rolledback:
            break
        await asyncio.sleep(0.01)

    assert conn.rolledback is True
    assert conn in pool.returned, "iade-ortasi iptal slot dusurmemeli"
    assert db_module._current_conn.get() is None
