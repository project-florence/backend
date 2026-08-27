"""Unit tests for `src/admin/__init__.py` -- TEST_COVERAGE_PLAN.md Adim D.

`admin_app` ayri bir FastAPI uygulamasi, yalniz Unix socket uzerinden
servis ediliyor (bkz. backend/AGENTS.md), TCP'ye hic acilmiyor;
`verify_admin_token` (src/api/deps.py) her endpoint'te `Depends` ile
uygulaniyor. Bu dosya iki katmani ayri tutuyor:

- "Gate" testleri: kapinin KENDISI -- gercek `X-Admin-Token` header'i ile,
  `dependency_overrides` KULLANMADAN, dogru/yanlis/eksik/yapilandirilmamis
  token senaryolarini her endpoint uzerinden dogrular.
- "Is mantigi" testleri: kapi `bypass_admin_gate` fixture'i ile
  `dependency_overrides` uzerinden atlanir (kapi zaten yukarida ayri test
  edildigi icin burada tekrar edilmiyor), yalniz handler'in kendi davranisi
  test edilir.

Hermetik: `fake_db`/`fake_redis` (tests/conftest.py) DB/Redis singleton'larini
in-memory sahtelerle degistirir; httpx `ASGITransport` ile `admin_app`
dogrudan cagrilir (ayri bir uvicorn/socket kurulumuna gerek yok).
"""

import inspect
from types import SimpleNamespace

import pytest

import src.admin as admin_module
from src.admin import admin_app
from src.api import deps as deps_module
from src.core import config as config_module
from src.core import database as db_module
from src.core import redis as redis_module

from api_helpers import request

ADMIN_HEADER = "X-Admin-Token"

# Her endpoint icin, kapi gecerse validasyonu da gececek minimum gecerli
# query/body -- boylece "yanlis token" testleri baska bir 4xx ile
# karismiyor, gercekten 403/500/422'nin KAPIDAN geldigini kanitliyor.
_ENDPOINT_CASES = [
    pytest.param("POST", "/gift-credits",
                 {"params": {"user_type": "everyone", "amount": 5}, "json": {}},
                 id="gift-credits"),
    pytest.param("POST", "/token-usage", {"json": {}}, id="token-usage"),
    pytest.param("POST", "/maintenance/toggle",
                 {"params": {"feature": "news", "action": "enable"}},
                 id="maintenance-toggle"),
    pytest.param("POST", "/config-reload", {}, id="config-reload"),
    pytest.param("POST", "/healthcheck", {}, id="healthcheck"),
]


@pytest.fixture
def admin_token(monkeypatch):
    """`deps_module.ADMIN_TOKEN`'i sabit bir degere ayarlar (gate testleri icin)."""
    monkeypatch.setattr(deps_module, "ADMIN_TOKEN", "test-admin-secret")
    return "test-admin-secret"


@pytest.fixture
def bypass_admin_gate():
    """`verify_admin_token` bagimliligini gecersiz kilar.

    SADECE is mantigi testlerinde kullanilir -- kapinin KENDISINI test
    ederken (asagidaki "gate" bolumu) KULLANILMAZ.
    """
    admin_app.dependency_overrides[deps_module.verify_admin_token] = lambda: True
    yield
    admin_app.dependency_overrides.pop(deps_module.verify_admin_token, None)


def _patch_llm(monkeypatch, result: bool):
    async def _fake(*a, **k):
        return result
    monkeypatch.setattr(admin_module, "health_check", _fake)


def _patch_news(monkeypatch, items):
    async def _fake(*a, **k):
        return items
    monkeypatch.setattr(admin_module, "news_search", _fake)


def _patch_news_raises(monkeypatch, exc):
    async def _fake(*a, **k):
        raise exc
    monkeypatch.setattr(admin_module, "news_search", _fake)


def _patch_yf(monkeypatch, info):
    monkeypatch.setattr(admin_module.yf, "Ticker", lambda symbol: SimpleNamespace(info=info))


class _FakeRedisConn:
    def __init__(self, ping_result=True):
        self._ping_result = ping_result

    async def ping(self):
        return self._ping_result


def _patch_redis_conn(monkeypatch, conn):
    async def _get_conn():
        return conn
    monkeypatch.setattr(redis_module.r, "_get_conn", _get_conn)


# ---------------------------------------------------------------------------
# Gate: verify_admin_token her endpoint'te uygulaniyor mu?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,kwargs", _ENDPOINT_CASES)
async def test_gate_wrong_token_rejected_on_every_endpoint(admin_token, method, path, kwargs):
    resp = await request(admin_app, method, path, headers={ADMIN_HEADER: "wrong-token"}, **kwargs)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid admin token"


@pytest.mark.parametrize("method,path,kwargs", _ENDPOINT_CASES)
async def test_gate_missing_header_rejected_on_every_endpoint(admin_token, method, path, kwargs):
    """`x_admin_token: str = Header(...)` zorunlu -- header hic yoksa
    verify_admin_token'a hic girilmeden FastAPI 422 doner."""
    resp = await request(admin_app, method, path, **kwargs)

    assert resp.status_code == 422


@pytest.mark.parametrize("method,path,kwargs", _ENDPOINT_CASES)
async def test_gate_admin_token_not_configured_returns_500_on_every_endpoint(
    monkeypatch, method, path, kwargs
):
    """ADMIN_TOKEN ortam degiskeni tanimsizken (None) her endpoint 500
    dondurur -- yanlislikla her header degerini kabul etmek yerine kapaniyor.
    Mevcut davranis budur, degistirilmedi."""
    monkeypatch.setattr(deps_module, "ADMIN_TOKEN", None)

    resp = await request(admin_app, method, path, headers={ADMIN_HEADER: "anything"}, **kwargs)

    assert resp.status_code == 500
    assert resp.json()["detail"] == "ADMIN_TOKEN not configured"


async def test_gate_correct_token_reaches_handler(admin_token, fake_redis):
    """Dogru token verildiginde kapi gecilir ve istek gercekten handler'a
    ulasir -- override KULLANILMADAN, ucdan uca (Header parse + verify_admin_token
    + is mantigi)."""
    resp = await request(
        admin_app, "POST", "/maintenance/toggle",
        headers={ADMIN_HEADER: admin_token},
        params={"feature": "news", "action": "disable"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"feature": "news", "disabled": True}


# ---------------------------------------------------------------------------
# POST /gift-credits
# ---------------------------------------------------------------------------


async def test_gift_credits_everyone_free_bucket(bypass_admin_gate, fake_db):
    fake_db.fetchall_result = [(1, "alice"), (2, "bob")]
    fake_db.fetchone_result = (None, None)  # _resolve_owner: bot degil

    resp = await request(
        admin_app, "POST", "/gift-credits",
        params={"user_type": "everyone", "amount": 10, "credit_type": "free_credits"},
        json={},
    )

    assert resp.status_code == 200
    assert resp.json() == {"success": True}
    insert_queries = [q for q, _ in fake_db.queries if "INSERT INTO user_credits" in q]
    assert len(insert_queries) == 2
    assert all("'free_credits'" in q for q in insert_queries)


async def test_gift_credits_everyone_gift_bucket(bypass_admin_gate, fake_db):
    fake_db.fetchall_result = [(1, "alice")]
    fake_db.fetchone_result = (None, None)

    resp = await request(
        admin_app, "POST", "/gift-credits",
        params={"user_type": "everyone", "amount": 7, "credit_type": "gift_credits"},
        json={},
    )

    assert resp.status_code == 200
    insert_queries = [q for q, _ in fake_db.queries if "INSERT INTO user_credits" in q]
    assert len(insert_queries) == 1
    assert "'gift_credits'" in insert_queries[0]


async def test_gift_credits_single_user_success(bypass_admin_gate, fake_db):
    # sirasiyla: SELECT id FROM users WHERE username=..., _resolve_owner (add),
    # _resolve_owner (get_total icinde), SUM sorgusu.
    fake_db.queue_fetchone((7,), (None, None), (None, None), (123.5,))

    resp = await request(
        admin_app, "POST", "/gift-credits",
        params={"user_type": "user", "username": "alice", "amount": 20},
        json={},
    )

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "user": {"username": "alice", "credits": 123.5}}


async def test_gift_credits_single_user_gift_bucket(bypass_admin_gate, fake_db):
    """user_type=user + credit_type=gift_credits kolu (line 66) -- yukaridaki
    test yalniz varsayilan free_credits'i kapsiyor."""
    fake_db.queue_fetchone((7,), (None, None), (None, None), (50.0,))

    resp = await request(
        admin_app, "POST", "/gift-credits",
        params={"user_type": "user", "username": "alice", "amount": 20, "credit_type": "gift_credits"},
        json={},
    )

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "user": {"username": "alice", "credits": 50.0}}
    insert_queries = [q for q, _ in fake_db.queries if "INSERT INTO user_credits" in q]
    assert len(insert_queries) == 1
    assert "'gift_credits'" in insert_queries[0]


async def test_gift_credits_user_not_found(bypass_admin_gate, fake_db):
    fake_db.queue_fetchone(None)

    resp = await request(
        admin_app, "POST", "/gift-credits",
        params={"user_type": "user", "username": "ghost", "amount": 5},
        json={},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found"


async def test_gift_credits_invalid_user_type(bypass_admin_gate, fake_db):
    resp = await request(
        admin_app, "POST", "/gift-credits",
        params={"user_type": "bogus", "amount": 5},
        json={},
    )

    assert resp.status_code == 400
    assert "Invalid type" in resp.json()["detail"]


async def test_gift_credits_user_type_requires_username(bypass_admin_gate, fake_db):
    resp = await request(
        admin_app, "POST", "/gift-credits",
        params={"user_type": "user", "amount": 5},
        json={},
    )

    assert resp.status_code == 400
    assert "username is required" in resp.json()["detail"]


@pytest.mark.parametrize("amount", [0, 1, -5])
async def test_gift_credits_invalid_amount_rejected(bypass_admin_gate, fake_db, amount):
    """`amount: int = Query(..., gt=1)` -- sifir, negatif VE sinir deger (1)
    422 ile reddedilir, handler'a hic girilmez."""
    resp = await request(
        admin_app, "POST", "/gift-credits",
        params={"user_type": "everyone", "amount": amount},
        json={},
    )

    assert resp.status_code == 422


async def test_gift_credits_unexpected_error_returns_500(bypass_admin_gate, fake_db, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("db patladi")
    monkeypatch.setattr(admin_module, "add_free_credits", _boom)
    fake_db.fetchall_result = [(1, "alice")]

    resp = await request(
        admin_app, "POST", "/gift-credits",
        params={"user_type": "everyone", "amount": 5},
        json={},
    )

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Database error"


# ---------------------------------------------------------------------------
# POST /token-usage
# ---------------------------------------------------------------------------


async def test_token_usage_defaults_no_group_by(bypass_admin_gate, fake_db):
    fake_db.fetchone_result = (3, 100, 200, 300)

    resp = await request(admin_app, "POST", "/token-usage", json={})

    assert resp.status_code == 200
    assert resp.json() == {
        "call_count": 3,
        "total_prompt_tokens": 100,
        "total_completion_tokens": 200,
        "total_tokens": 300,
    }


async def test_token_usage_new_filter_params_reach_query(bypass_admin_gate, fake_db):
    """REFACTOR_PLAN.md Adim 3'te eklenen purpose/provider/model/status
    parametreleri gercekten WHERE kosuluna ve sorgu parametrelerine ulasiyor."""
    fake_db.fetchone_result = (1, 10, 20, 30)

    resp = await request(
        admin_app, "POST", "/token-usage",
        params={"purpose": "digest", "provider": "anthropic", "model": "claude-x", "status": "ok"},
    )

    assert resp.status_code == 200
    query, params = fake_db.queries[-1]
    assert "purpose = %s" in query
    assert "provider = %s" in query
    assert "model = %s" in query
    assert "status = %s" in query
    assert params == ["digest", "anthropic", "claude-x", "ok"]


async def test_token_usage_group_by_provider_returns_breakdown(bypass_admin_gate, fake_db):
    fake_db.fetchone_result = (5, 50, 60, 110)
    fake_db.fetchall_result = [("anthropic", 3, 30, 40, 70), ("openai", 2, 20, 20, 40)]

    resp = await request(admin_app, "POST", "/token-usage", params={"group_by": "provider"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["breakdown"][0] == {
        "group_by": "provider",
        "value": "anthropic",
        "call_count": 3,
        "total_prompt_tokens": 30,
        "total_completion_tokens": 40,
        "total_tokens": 70,
    }


async def test_token_usage_invalid_group_by_returns_400_not_500(bypass_admin_gate, fake_db):
    """Regresyon kilidi: `get_token_summary` allowlist disi group_by icin
    HTTPException(400) firlatir; admin endpoint'teki `except HTTPException:
    raise` dali bunu genel `except Exception: 500`'e dusurmemeli."""
    fake_db.fetchone_result = (0, 0, 0, 0)

    resp = await request(admin_app, "POST", "/token-usage", params={"group_by": "bogus"})

    assert resp.status_code == 400
    assert "Invalid group_by" in resp.json()["detail"]


async def test_token_usage_invalid_since_datetime_returns_400(bypass_admin_gate, fake_db):
    resp = await request(admin_app, "POST", "/token-usage", params={"since": "not-a-date"})

    assert resp.status_code == 400
    assert "Invalid datetime format" in resp.json()["detail"]


async def test_token_usage_unexpected_error_returns_500(bypass_admin_gate, fake_db, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(admin_module, "get_token_summary", _boom)

    resp = await request(admin_app, "POST", "/token-usage", json={})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Database error"


# ---------------------------------------------------------------------------
# POST /maintenance/toggle
# ---------------------------------------------------------------------------


async def test_maintenance_toggle_disable(bypass_admin_gate, fake_redis):
    resp = await request(
        admin_app, "POST", "/maintenance/toggle",
        params={"feature": "report_generate", "action": "disable"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"feature": "report_generate", "disabled": True}
    assert "report_generate" in fake_redis.store.get("maintenance:disabled", set())


async def test_maintenance_toggle_enable(bypass_admin_gate, fake_redis):
    fake_redis.store["maintenance:disabled"] = {"report_generate"}

    resp = await request(
        admin_app, "POST", "/maintenance/toggle",
        params={"feature": "report_generate", "action": "enable"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"feature": "report_generate", "disabled": False}
    assert "report_generate" not in fake_redis.store.get("maintenance:disabled", set())


async def test_maintenance_toggle_unknown_feature(bypass_admin_gate, fake_redis):
    resp = await request(
        admin_app, "POST", "/maintenance/toggle",
        params={"feature": "bogus-feature", "action": "enable"},
    )

    assert resp.status_code == 400
    assert "Unknown feature" in resp.json()["detail"]


async def test_maintenance_toggle_invalid_action(bypass_admin_gate, fake_redis):
    resp = await request(
        admin_app, "POST", "/maintenance/toggle",
        params={"feature": "news", "action": "bogus-action"},
    )

    assert resp.status_code == 400
    assert "Action must be" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /config-reload
# ---------------------------------------------------------------------------


async def test_config_reload_calls_reload_config(bypass_admin_gate, monkeypatch):
    calls = []
    monkeypatch.setattr(admin_module, "reload_config", lambda: calls.append(1))

    resp = await request(admin_app, "POST", "/config-reload")

    assert resp.status_code == 200
    assert resp.json() == {"success": True}
    assert calls == [1]


async def test_config_reload_error_returns_500(bypass_admin_gate, monkeypatch):
    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(admin_module, "reload_config", _boom)

    resp = await request(admin_app, "POST", "/config-reload")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal server error"


def test_config_reload_does_not_re_read_dotenv():
    """Belgeleme (urun kodu degistirilmedi): `reload_config()` sadece
    `config = None` yapip `init_config()`'u tekrar cagirir -- bu da mevcut
    `os.environ`'dan yeniden okur ama `load_dotenv()`'i TEKRAR cagirmaz.
    Yani `.env` dosyasi disk uzerinde degisse bile, calisan surecin
    `os.environ`'u guncellenmeden `/config-reload` pratikte no-op'a
    yakindir. Kaynagi denetleyerek dogrulaniyor (mock'lu bir DB/Redis
    davranisiyla degil, cunku reload_config hicbirini kullanmiyor)."""
    source = inspect.getsource(config_module.reload_config)

    assert "load_dotenv" not in source


# ---------------------------------------------------------------------------
# POST /healthcheck
# ---------------------------------------------------------------------------


async def test_healthcheck_all_healthy(bypass_admin_gate, fake_db, monkeypatch):
    fake_db.fetchone_result = (1,)
    _patch_llm(monkeypatch, True)
    _patch_news(monkeypatch, [{"title": "x"}])
    _patch_yf(monkeypatch, {"regularMarketPrice": 100})
    _patch_redis_conn(monkeypatch, _FakeRedisConn(True))

    resp = await request(admin_app, "POST", "/healthcheck")

    assert resp.status_code == 200
    assert resp.json() == {
        "db_health": True,
        "redis_health": True,
        "llm_health": True,
        "news_health": True,
        "yfinance_health": True,
        "status": "OK",
    }


async def test_healthcheck_reports_each_probe_independently_on_value_failure(
    bypass_admin_gate, fake_db, monkeypatch
):
    """Her prob DEGER bazinda (exception degil) basarisiz olursa hepsi
    kendi basina raporlanir -- birinin False donmesi digerlerinin
    calisip raporlanmasini engellemez."""
    fake_db.fetchone_result = (0,)  # db check "basarisiz" deger, exception degil
    _patch_llm(monkeypatch, False)
    _patch_news(monkeypatch, [])  # bos liste -> news_health False
    _patch_yf(monkeypatch, None)  # info None -> yfinance_health False
    _patch_redis_conn(monkeypatch, _FakeRedisConn(False))

    resp = await request(admin_app, "POST", "/healthcheck")

    assert resp.status_code == 200
    assert resp.json() == {
        "db_health": False,
        "redis_health": False,
        "llm_health": False,
        "news_health": False,
        "yfinance_health": False,
        "status": "ERROR",
    }


async def test_healthcheck_redis_exception_is_caught_others_still_reported(
    bypass_admin_gate, fake_db, monkeypatch
):
    """Redis probu TEK (ozel) try/except'e sahip: gercek bir baglanti
    hatasi (exception) firlarsa sessizce redis_health=False'a cevrilir,
    digger problar etkilenmeden calismaya devam eder."""
    fake_db.fetchone_result = (1,)
    _patch_llm(monkeypatch, True)
    _patch_news(monkeypatch, [{"title": "x"}])
    _patch_yf(monkeypatch, {"p": 1})

    async def _boom_get_conn():
        raise RuntimeError("redis soket hatasi")
    monkeypatch.setattr(redis_module.r, "_get_conn", _boom_get_conn)

    resp = await request(admin_app, "POST", "/healthcheck")

    assert resp.status_code == 200
    body = resp.json()
    assert body["redis_health"] is False
    assert body["db_health"] is True
    assert body["llm_health"] is True
    assert body["news_health"] is True
    assert body["yfinance_health"] is True
    assert body["status"] == "ERROR"


async def test_healthcheck_db_probe_uncaught_exception_still_releases_connection(
    bypass_admin_gate, fake_db, monkeypatch
):
    """Belgelenen davranis (urun kodu DEGISTIRILMEDI -- ameliyat gerektirir):
    db check'in redis'in aksine try/except'i yok. Gercek bir DB hatasi
    (deger degil, exception) firlarsa TUM /healthcheck coker -- raw
    exception, HTTPException DEGIL, dolayisiyla 'diger problar yine de
    raporlanir' garantisi bu durumda GECERSIZDIR. Yine de
    `admin_release_middleware`'in finally'i calisir (baglanti sizmiyor) --
    admin/__init__.py'nin kendi docstring'inin vaat ettigi sey budur ve
    dogrulaniyor."""
    def _boom_cursor(*a, **k):
        raise RuntimeError("db baglanti hatasi")
    monkeypatch.setattr(db_module.db, "cursor", _boom_cursor)

    with pytest.raises(RuntimeError, match="db baglanti hatasi"):
        await request(admin_app, "POST", "/healthcheck")

    assert fake_db.release_calls == 1


async def test_healthcheck_news_probe_uncaught_exception_also_propagates(
    bypass_admin_gate, fake_db, monkeypatch
):
    """Ayni desen news probunda da tekrarlaniyor -- db'ye ozgu degil,
    yalniz redis probu ozel olarak korunmus."""
    fake_db.fetchone_result = (1,)
    _patch_redis_conn(monkeypatch, _FakeRedisConn(True))
    _patch_llm(monkeypatch, True)
    _patch_news_raises(monkeypatch, RuntimeError("searxng kapali"))

    with pytest.raises(RuntimeError, match="searxng kapali"):
        await request(admin_app, "POST", "/healthcheck")


# ---------------------------------------------------------------------------
# admin_release_middleware -- her istekten sonra db.release_current()
# ---------------------------------------------------------------------------


async def test_admin_release_middleware_called_after_success(bypass_admin_gate, fake_db, fake_redis):
    resp = await request(
        admin_app, "POST", "/maintenance/toggle",
        params={"feature": "news", "action": "enable"},
    )

    assert resp.status_code == 200
    assert fake_db.release_calls == 1


async def test_admin_release_middleware_called_after_handled_error(bypass_admin_gate, fake_db, fake_redis):
    resp = await request(
        admin_app, "POST", "/maintenance/toggle",
        params={"feature": "bogus-feature", "action": "enable"},
    )

    assert resp.status_code == 400
    assert fake_db.release_calls == 1
