"""``src/clients/cron.py::CronClient._claim`` icin gercek Redis'e karsi
calisan, opt-in entegrasyon testleri.

TEST_COVERAGE_PLAN.md Adim C: ``_claim``'in SET NX + TTL sozlesmesi
``FakeRedis`` (tests/api_helpers.py) tarafindan tam olarak taklit
edilemiyor -- ``FakeRedis.set`` ``ex``'i saklamiyor/sure asimini simule
etmiyor. Bu dosya SADECE bu gercek-Redis-semantigi bosluğunu kapatir;
``_claim``'in geri kalan mantigi (fallback kilit, TTL hesabi, is
zamanlamasi) tests/test_cron_client.py'de tamamen hermetik test edilir.

Ayrica burada dogrulanan gercek NX davranisi (``set(nx=True)`` anahtar
zaten varsa da ``None`` doner -- "Redis erisilemez" ile AYNI sinyal),
src/clients/cron.py::_claim'de bulunan ve duzeltilen gercek bir hatanin
kanitidir (bkz. tests/test_cron_client.py docstring'i ve rapor).

Calistirma
----------
    cd backend && source .venv/bin/activate
    docker compose up -d redis   # florence_redis, bkz. docker-compose.yml
    python -m pytest -m integration -q tests/test_cron_integration.py

Container ayakta degilse testler HATA vermez, temiz bir ``pytest.skip`` ile
gecilir. Prod'a asla baglanilmaz (``_guard_not_local``, aynen
tests/test_llm_integration.py'deki gibi).
"""

import asyncio
import os
import uuid

import pytest
import redis.asyncio as aioredis

from src.clients.cron import PREFIX, CronClient
from src.core import redis as redis_module

pytestmark = pytest.mark.integration

_CONNECT_TIMEOUT = 2.0


def _guard_not_local() -> str | None:
    """Prod'a asla baglanmama guvenlik agi -- tests/test_llm_integration.py ile ayni desen."""
    if os.getenv("FLORENCE_INTEGRATION_ALLOW_REMOTE") == "1":
        return None
    host = os.getenv("REDIS_HOST", "")
    if host not in ("localhost", "127.0.0.1", ""):
        return (
            f"REDIS_HOST={host!r} yerel gorunmuyor; entegrasyon testleri prod'a "
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
        pytest.skip(f"entegrasyon container'i ayakta degil, atlaniyor -- {failure}")


@pytest.fixture(autouse=True)
async def _reset_singleton(_integration_target):
    """Her testte taze bir Redis baglantisi -- pytest-asyncio her test icin
    yeni bir event loop aciyor, onceki testin baglantisi o loop'a bagli kalir
    (bkz. tests/test_llm_integration.py::_reset_singletons ayni gerekce)."""
    yield
    conn = redis_module.r._conn
    if conn is not None:
        try:
            await conn.aclose()
        except Exception:
            pass
        redis_module.r._conn = None
        redis_module.r._disabled = False


@pytest.fixture
async def _cleanup_keys():
    keys: list[str] = []
    yield keys
    if keys:
        await redis_module.r.delete(*keys)


def _unique_name(label: str) -> str:
    return f"itest-cron-{label}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# SET NX + TTL sozlesmesi
# ---------------------------------------------------------------------------


async def test_claim_sets_real_key_with_ttl_matching_interval(_cleanup_keys):
    name = _unique_name("ttl")
    lock_key = f"{PREFIX}lock:{name}"
    _cleanup_keys.append(lock_key)

    client = CronClient()
    result = await client._claim(name, interval_ms=5_000)  # ttl = 5s

    assert result is True
    conn = await redis_module.r._get_conn()
    assert conn is not None
    ttl = await conn.ttl(lock_key)
    assert 0 < ttl <= 5
    value = await conn.get(lock_key)
    assert value == "1"


async def test_claim_blocks_second_worker_via_real_nx(_cleanup_keys):
    """GERCEK REGRESYON TESTI: Redis saglikliyken kilit baskasindaysa ikinci
    worker isi CALISTIRMAMALI (duzeltilen gercek hata, bkz. dosya docstring'i)."""
    name = _unique_name("nx")
    lock_key = f"{PREFIX}lock:{name}"
    _cleanup_keys.append(lock_key)

    first_worker = CronClient()
    second_worker = CronClient()  # ayri bir CronClient orneği = ayri "worker" simulasyonu

    first = await first_worker._claim(name, interval_ms=10_000)
    second = await second_worker._claim(name, interval_ms=10_000)

    assert first is True
    assert second is False
    # ikinci worker yerel fallback'e DUSMEMELI -- Redis erisilebilirdi.
    assert second_worker._fallback_locks == {}


async def test_claim_reclaimable_after_real_ttl_expiry(_cleanup_keys):
    name = _unique_name("expiry")
    lock_key = f"{PREFIX}lock:{name}"
    _cleanup_keys.append(lock_key)

    client = CronClient()
    # Kisa TTL (1s) -- gercek zaman asimini beklemek entegrasyon seridinde
    # kabul edilebilir (hermetik seritte YASAK, burada degil).
    first = await client._claim(name, interval_ms=1_000)
    assert first is True

    immediate_retry = await client._claim(name, interval_ms=1_000)
    assert immediate_retry is False

    await asyncio.sleep(1.3)  # TTL'in gercekten dolmasini bekle

    after_expiry = await client._claim(name, interval_ms=1_000)
    assert after_expiry is True
