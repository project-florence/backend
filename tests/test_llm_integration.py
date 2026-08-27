"""Gercek Postgres + Redis'e karsi calisan, opt-in entegrasyon test katmani.

REFACTOR_PLAN.md Adim 6. Geri kalan paket (550+ test, ``tests/test_llm_*.py``
dahil) tamamen hermetik: ``fake_db``/``fake_redis`` gercek baglanti kurmaz.
Bu dosya BILEREK farkli -- REFACTOR_PLAN.md'nin 0. bolumunde anlatilan
2026-08-26 arizasinin arastirmasinda cikan uc gercek hatanin UCU DE mock'lu
paket tarafindan yapisal olarak gorulemezdi:

    1. Onceki bir taslakta ``build_agent`` senkron bir kopru (worker
       thread'de ``asyncio.run``) uzerinden cozumleme yapiyordu -- bu, ana
       event loop'a bagli ``AsyncConnectionPool`` ve async Redis istemcisine
       YABANCI bir loop'tan dokunuyordu. ``fake_db``/``fake_redis`` hicbir
       loop bagi tasimadigi icin bu sinifi hatayi goremez.
    2. ``llm_settings.provider`` -> ``llm_providers`` FK'si yuzunden
       anahtarsiz saglayicilar bile ``llm_providers``'ta bir SATIR
       gerektiriyor; bu sadece gercek Postgres'in FK kisitini
       uygularken ortaya cikar (fake_db kisit uygulamaz).
    3. ``opencode-zen`` ``/models``'i anahtarsiz veriyor ama
       ``/chat/completions`` 401 donuyor -- bu ag/gercek-saglayici davranisi,
       bu dosyanin kapsami DISINDA (asagidaki "Yapilmayanlar"a bak) ama
       katalogda ``verified``/``notes`` alanlariyla belgelendi.

Bu dosya (1) ve (2)'nin REGRESYON testlerini icerir + init_db idempotency,
sifreli anahtar round-trip, Redis onbellek/TTL/gecersiz kilma ve
token_usage gozlemlenebilirligi icin gercek-altyapili kapsama ekler.

Calistirma
----------
Varsayilan ``python -m pytest`` bu dosyadaki testleri CALISTIRMAZ --
``pyproject.toml``'daki ``addopts = -m "not integration"`` bunu saglar.
Acikca calistirmak icin::

    cd backend && source .venv/bin/activate
    docker compose up -d postgres redis   # florence_postgres / florence_redis
    python -m pytest -m integration -q

Container'lar kapaliysa testler HATA vermez, temiz bir ``pytest.skip`` ile
gecilir (baglanti denemesi ``_integration_target`` fixture'inda yapilir).

Hedef / guvenlik agi
---------------------
Hedef host/port'lar standart ``POSTGRES_HOST``/``POSTGRES_PORT``/
``REDIS_HOST``/``REDIS_PORT`` ortam degiskenleridir (``.env`` -> yerel dev
container'lari: Postgres localhost:5433, Redis localhost:5434; bkz.
docker-compose.yml). Ayni degiskenler ``src/core/database.py`` ve
``src/core/redis.py``'nin zaten okudugu degiskenler -- ayri bir "entegrasyon
env'i" icat edilmedi. PROD'A ASLA BAGLANILMAMASI icin ``_guard_not_local``
host localhost/127.0.0.1 disindaysa testleri sessizce skip eder (bilerek
uzak bir hedef icin ``FLORENCE_INTEGRATION_ALLOW_REMOTE=1`` gerekir).

Temizlik
--------
Her test kendi olusturdugu satirlari ``_cleanup`` fixture'i uzerinden
teardown'da siler (``llm_settings`` -> ``llm_providers`` sirasiyla, FK
kisitina uymak icin). ``token_usage``'a yazilan satirlar test-basina benzersiz
bir ``purpose`` degeriyle (ornek: ``itest-log-<rastgele>``) isaretlenir ve
sadece o deger silinir -- gercek/onceki ``token_usage`` verisine (bu dev
DB'de calistirma aninda 108 satir vardi) DOKUNULMAZ. Ikinci bir kosuda da
testler ayni sekilde gecer (idempotent).

Yapilmayanlar
-------------
Gercek ag/saglayici cagrisi YOK (``opencode-zen`` 401 bulgusu gibi seyler
bu katmanin degil, canli-saglayici dogrulamasinin isi -- REFACTOR_PLAN.md
"Acik dogrulama borcu"). TUI yok (kullanici karari, Adim 6 kapsam disi).
"""

import asyncio
import base64
import inspect
import os
import uuid
from types import SimpleNamespace

import psycopg
import pytest
import redis.asyncio as aioredis
from fastapi import HTTPException
from pydantic_ai.models.openai import OpenAIChatModel

import src.llm.agents as llm_agents
import src.llm.crypto as crypto
import src.llm.settings as llm_settings
import src.services.token as token_service
from src.core import database as database_module
from src.core import redis as redis_module
from src.core.database import init_db

pytestmark = pytest.mark.integration

_CONNECT_TIMEOUT = 2.0


def _guard_not_local() -> str | None:
    """Prod'a asla baglanmama guvenlik agi -- bkz. dosya docstring'i."""
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
    """Container'lara baglanabiliyor muyuz? Yoksa TUM modul skip (hata degil).

    Bagimsiz, kisa timeout'lu bir prob baglantisi kurar (uygulamanin gercek
    havuzunu/redis proxy'sini hic olusturmadan) -- basarisizsa ``pytest.skip``.
    Basariliysa asagidaki testler gercek ``db``/``r`` singleton'larini
    kullanir (ayni env zaten docker-compose ile eslesiyor).
    """
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
        try:
            client = aioredis.Redis(
                host=os.getenv("REDIS_HOST"),
                port=int(os.getenv("REDIS_PORT", "6379")),
                db=int(os.getenv("REDIS_DB", "0")),
                password=os.getenv("REDIS_PASSWORD") or None,
                socket_connect_timeout=_CONNECT_TIMEOUT,
                socket_timeout=_CONNECT_TIMEOUT,
            )
            await client.ping()
            await client.aclose()
        except Exception as exc:
            return f"Redis'e baglanilamadi (REDIS_PORT={os.getenv('REDIS_PORT')}): {exc}"
        return None

    failure = asyncio.run(_probe())
    if failure:
        pytest.skip(f"entegrasyon container'lari ayakta degil, atlaniyor -- {failure}")


@pytest.fixture(autouse=True)
async def _reset_singletons(_integration_target):
    """Her testte TAZE havuz/redis baglantisi kurulmasini zorlar.

    ``db``/``r`` surec-omurlu singleton'lar; pytest-asyncio her test
    fonksiyonu icin YENI bir event loop aciyor (pyproject.toml:
    asyncio_default_fixture_loop_scope = "function"). Bir onceki testte
    kurulan havuz/redis baglantisi o testin loop'una bagli kalirsa bu
    testin loop'unda kullanilamaz ("attached to a different loop") -- tam
    olarak REFACTOR_PLAN.md Adim 2'deki hata #1'in sinifi. Testten SONRA
    kapatarak bir sonraki test kendi loop'unda taze kurulum yapmaya
    zorlanir; boylece build_agent testi (asagida) her zaman KENDI loop'una
    bagli bir havuzla calisir.
    """
    yield
    await database_module.db.close()
    conn = redis_module.r._conn
    if conn is not None:
        try:
            await conn.aclose()
        except Exception:
            pass
        redis_module.r._conn = None
        redis_module.r._disabled = False


@pytest.fixture
async def _cleanup():
    """Test-basina temizlik kaydi: llm_settings -> llm_providers -> token_usage.

    Sira onemli: ``llm_settings.provider`` FK'si yuzunden bir amac hala bir
    saglayiciyi kullaniyorken o saglayici satiri silinemez.
    """
    state = SimpleNamespace(purposes=[], providers=[], token_usage_purposes=[])
    yield state
    for purpose in state.purposes:
        try:
            await llm_settings.clear_selection(purpose)
        except Exception:
            pass
    for provider_id in state.providers:
        try:
            await llm_settings.remove_provider(provider_id)
        except Exception:
            pass
    if state.token_usage_purposes:
        async with database_module.db.cursor(row_factory=None) as cur:
            await cur.execute(
                "DELETE FROM token_usage WHERE purpose = ANY(%s)",
                (state.token_usage_purposes,),
            )
            await database_module.db.commit()


# ---------------------------------------------------------------------------
# init_db(): gercek tablolar + idempotency
# ---------------------------------------------------------------------------


async def test_init_db_creates_llm_tables_and_is_idempotent():
    await init_db()
    await init_db()  # ikinci calistirma patlamamali

    async with database_module.db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'llm_providers'"
        )
        provider_cols = {row[0] for row in await cur.fetchall()}
        await cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'llm_settings'"
        )
        settings_cols = {row[0] for row in await cur.fetchall()}
        await cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'token_usage'"
        )
        usage_cols = {row[0] for row in await cur.fetchall()}

    assert {"provider", "api_key_encrypted", "base_url", "enabled"} <= provider_cols
    assert {"purpose", "provider", "model", "params", "updated_by"} <= settings_cols
    assert {"purpose", "provider", "status", "error", "duration_ms"} <= usage_cols


# ---------------------------------------------------------------------------
# Sifreli anahtar: gercek BYTEA round-trip + AAD uyusmazligi
# ---------------------------------------------------------------------------


async def test_provider_key_round_trips_through_real_bytea_column(monkeypatch, _cleanup):
    monkeypatch.setenv("FLORENCE_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    _cleanup.providers.append("mistral")
    await llm_settings.remove_provider("mistral")  # onceki kalinti icin best-effort temizlik

    await llm_settings.upsert_provider("mistral", api_key="sk-integration-real-key-000111")
    row = await llm_settings.get_provider_row("mistral")
    assert row is not None
    encrypted = bytes(row["api_key_encrypted"])
    assert b"sk-integration-real-key-000111" not in encrypted  # gercekten sifreli, duz metin degil

    decrypted = crypto.decrypt(encrypted, aad="mistral")
    assert decrypted == "sk-integration-real-key-000111"

    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt(encrypted, aad="anthropic")  # yanlis AAD -> cozme basarisiz


# ---------------------------------------------------------------------------
# set_selection: FK ihlali (hata #2'nin regresyonu)
# ---------------------------------------------------------------------------


async def test_set_selection_unknown_provider_raises_fk_violation(_cleanup):
    _cleanup.purposes.append("digest")
    await llm_settings.remove_provider("xai")  # temiz baslangic garantisi

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await llm_settings.set_selection("digest", "xai", "grok-test-model")

    # Basarisiz INSERT commit edilmedi -- llm_settings'te satir olusmamali.
    row = await llm_settings.get_selection("digest")
    assert row is None


# ---------------------------------------------------------------------------
# resolve_purpose: gercek Redis onbellegi (okuma / bayatlik / gecersiz kilma / TTL)
# ---------------------------------------------------------------------------


async def test_resolve_purpose_uses_real_redis_cache_and_invalidates_on_write(_cleanup):
    _cleanup.purposes.append("digest")
    _cleanup.providers.append("ollama-local")

    await llm_settings.upsert_provider("ollama-local")  # keyless -- FK icin satir sart
    await llm_settings.set_selection("digest", "ollama-local", "llama-cache-v1")

    resolved1 = await llm_settings.resolve_purpose("digest")
    assert isinstance(resolved1, llm_settings.ResolvedLLM)
    assert resolved1.model == "llama-cache-v1"

    cache_key = llm_settings._cache_key("digest")
    conn = await redis_module.r._get_conn()
    assert conn is not None, "Redis baglantisi kurulamadi (container ayakta olmali)"
    ttl = await conn.ttl(cache_key)
    assert 0 < ttl <= 60  # REFACTOR_PLAN.md 2.4: "TTL <= 60s"

    # DB'yi set_selection'i BYPASS ederek dogrudan degistir: onbellek
    # gecersiz kilinmaz, ikinci resolve_purpose cagrisi hala ONBELLEKTEKI
    # (eski) modeli donmeli.
    async with database_module.db.cursor(row_factory=None) as cur:
        await cur.execute(
            "UPDATE llm_settings SET model = %s WHERE purpose = %s",
            ("llama-should-not-be-seen-yet", "digest"),
        )
        await database_module.db.commit()

    resolved2 = await llm_settings.resolve_purpose("digest")
    assert isinstance(resolved2, llm_settings.ResolvedLLM)
    assert resolved2.model == "llama-cache-v1"  # hala onbellekten (bayat DB okunmadi)

    # set_selection DOGRUDAN gecersiz kilar -- degisiklik hemen gorunmeli.
    await llm_settings.set_selection("digest", "ollama-local", "llama-cache-v2")
    resolved3 = await llm_settings.resolve_purpose("digest")
    assert isinstance(resolved3, llm_settings.ResolvedLLM)
    assert resolved3.model == "llama-cache-v2"


# ---------------------------------------------------------------------------
# build_agent(): gercek event loop + gercek havuz (hata #1'in regresyonu)
# ---------------------------------------------------------------------------


async def test_build_agent_runs_on_real_event_loop_and_pool(_cleanup):
    _cleanup.purposes.append("report")
    _cleanup.providers.append("ollama-local")

    # Bu ASSERT bilerek burada: bir sonraki degisiklik build_agent'i tekrar
    # senkron bir koprunun (worker thread + asyncio.run) arkasina saklarsa
    # bu test kirilmali (bkz. src/llm/agents.py modul docstring'i).
    assert inspect.iscoroutinefunction(llm_agents.build_agent)

    await llm_settings.upsert_provider("ollama-local")
    await llm_settings.set_selection("report", "ollama-local", "llama-agent-test")

    built = await llm_agents.build_agent("report")
    assert isinstance(built, llm_agents.BuiltAgent)
    assert isinstance(built.model, OpenAIChatModel)
    assert built.model_name == "llama-agent-test"
    assert built.provider_id == "ollama-local"
    # ollama-local: reasoning_param=None -> hicbir reasoning ayari gonderilmemeli.
    assert built.model_settings == {}


# ---------------------------------------------------------------------------
# log_llm_call(): gercek tabloya basari + hata satiri, hata metni sanitize
# ---------------------------------------------------------------------------


async def test_log_llm_call_writes_success_and_sanitized_error_rows(_cleanup):
    purpose = f"itest-log-{uuid.uuid4().hex[:8]}"
    _cleanup.token_usage_purposes.append(purpose)

    await llm_agents.log_llm_call(
        purpose=purpose,
        model_name="itest-model-ok",
        provider_id="groq",
        status="ok",
        duration_ms=123,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    await llm_agents.log_llm_call(
        purpose=purpose,
        model_name="itest-model-err",
        provider_id="groq",
        status="error",
        duration_ms=45,
        error=RuntimeError(
            "401 upstream: Authorization: Bearer sk-live-shouldnotleak123 rejected"
        ),
    )

    async with database_module.db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT model, status, error, prompt_tokens, completion_tokens, total_tokens "
            "FROM token_usage WHERE purpose = %s ORDER BY id",
            (purpose,),
        )
        rows = await cur.fetchall()

    assert len(rows) == 2
    ok_row, err_row = rows
    assert ok_row[1] == "ok"
    assert ok_row[2] is None
    assert (ok_row[3], ok_row[4], ok_row[5]) == (10, 5, 15)

    assert err_row[1] == "error"
    assert err_row[3] is None and err_row[4] is None and err_row[5] is None  # basarisiz cagri: token sayisi yok
    assert "sk-live-shouldnotleak123" not in (err_row[2] or "")
    assert "RuntimeError" in err_row[2]


# ---------------------------------------------------------------------------
# get_token_summary(): gercek SQL group_by kirilimi + allowlist reddi
# ---------------------------------------------------------------------------


async def test_get_token_summary_group_by_and_allowlist(_cleanup):
    purpose = f"itest-summary-{uuid.uuid4().hex[:8]}"
    _cleanup.token_usage_purposes.append(purpose)

    await token_service.log_token_usage(
        "model-a", purpose=purpose, provider="openai",
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
    )
    await token_service.log_token_usage(
        "model-a", purpose=purpose, provider="openai",
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
    )
    await token_service.log_token_usage(
        "model-b", purpose=purpose, provider="anthropic",
        prompt_tokens=30, completion_tokens=10, total_tokens=40,
    )

    summary = await token_service.get_token_summary(purpose=purpose, group_by="provider")
    assert summary["call_count"] == 3
    assert summary["total_tokens"] == 205

    breakdown = {row["value"]: row for row in summary["breakdown"]}
    assert breakdown["openai"]["call_count"] == 2
    assert breakdown["openai"]["total_tokens"] == 165
    assert breakdown["anthropic"]["call_count"] == 1
    assert breakdown["anthropic"]["total_tokens"] == 40

    with pytest.raises(HTTPException) as exc_info:
        await token_service.get_token_summary(purpose=purpose, group_by="not-an-allowed-column")
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Baglanti sizintisi kontrolu
# ---------------------------------------------------------------------------


async def test_no_connection_leak_after_repeated_operations(_cleanup):
    purpose = f"itest-leak-{uuid.uuid4().hex[:8]}"
    _cleanup.token_usage_purposes.append(purpose)

    for i in range(15):
        await token_service.log_token_usage(
            f"model-leak-{i}", purpose=purpose, provider="openai",
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
        )

    await asyncio.sleep(0.05)  # havuzun periyodik check-callback'i icin kucuk pay
    stats = database_module._pool.get_stats()
    checked_out = stats["pool_size"] - stats["pool_available"]
    assert checked_out <= 1, (
        f"havuzda acik/iade edilmemis baglanti kaldi: {stats} -- portfolio'daki "
        "keep=True cursor sizintisina benzer bir sizinti olabilir "
        "(bkz. CLAUDE.md 'Portfoy tek JSONB blob')"
    )
    assert stats["pool_size"] <= int(os.getenv("POSTGRES_POOL_MAX", "10"))
