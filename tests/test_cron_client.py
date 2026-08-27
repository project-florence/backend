"""``src/clients/cron.py`` (CronClient) icin hermetik testler.

TEST_COVERAGE_PLAN.md Adim C: 2026-08-26 arizasinda zamanlayici katmani hic
test edilmemisti. Bu dosya kapsar:

1. ``_is_due`` zamanlama siniri (``last_run + interval_ms <= now``, ``last_run``
   None iken davranis).
2. Redis kilidi (``_claim``): alinmasi, TTL'in ``interval_ms/1000`` olmasi,
   kilit alinamadiginda False donmesi, kilidin (TTL ile) birakilmasi.
3. ``_running`` seti: ayni is bitmeden ikinci kez tetiklenmiyor.
4. Bir is hata verdiginde: istisna yakalaniyor mu, ``last_run`` yine de
   guncelleniyor mu, diger isler etkileniyor mu, is devre disi mi kaliyor.
5. ``exec()`` ile calistirma (``_run_code``): ``__cron_main__`` sozlesmesi,
   eski (sync) format, bozuk kod.
6. ``last_run`` DB'ye ne zaman yaziliyor -- is basinda mi bitince mi.

GERCEK HATA (bulundu ve duzeltildi -- bkz. rapor): ``_claim``, redis-py'nin
``set(..., nx=True)``'in HEM "Redis'e erisilemedi" HEM DE "anahtar zaten var
(kilit baska worker'da)" durumunda ayni sekilde ``None`` dondurdugunu ayirt
etmiyordu; ikinci durumda bile surec-ici fallback kilide dusup isi yine de
calistiriyordu. Duzeltme: ``r._conn is not None`` ile ayirt et (bkz.
src/clients/cron.py::_claim). Bu dosyadaki ``test_claim_fails_when_already_locked_in_redis``
ve ``test_claim_returns_false_when_real_redis_lock_is_held_by_another_worker``
regresyon testleridir; gercek Redis'e karsi TTL/NX dogrulamasi icin ayrica
bkz. tests/test_cron_integration.py.
"""

import asyncio
import sys
import time
import types
from datetime import UTC, datetime, timedelta

import pytest

from src.clients import cron as cron_module
from src.clients.cron import PREFIX, CronClient, Job
from src.core import redis as redis_module


# ---------------------------------------------------------------------------
# Yardimcilar
# ---------------------------------------------------------------------------


class _FixedDatetime(datetime):
    """``cron_module.datetime``'i dondurmek icin -- gercek sleep KULLANILMAZ."""

    _fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def _freeze_now(monkeypatch, when: datetime) -> None:
    _FixedDatetime._fixed = when
    monkeypatch.setattr(cron_module, "datetime", _FixedDatetime)


@pytest.fixture
def probe(monkeypatch):
    """exec() edilen kaynak kodun disariya sinyal gonderebilmesi icin sahte modul.

    ``_run_code``'un ``ns`` sozlugu disaridan hicbir referans almiyor (sadece
    ``__cron_name__``); exec edilen kod ``import test_cron_probe as _p`` ile
    bu module erisip yan etkisini gozlemlenebilir kilar. ``monkeypatch``
    testten sonra ``sys.modules``'i geri alir.
    """
    mod = types.ModuleType("test_cron_probe")
    mod.calls = []
    monkeypatch.setitem(sys.modules, "test_cron_probe", mod)
    return mod


def _always_true_conn(monkeypatch):
    """Redis 'erisilebilir' durumunu simule et (``r._conn`` dolu)."""
    monkeypatch.setattr(redis_module.r, "_conn", object())


def _no_conn(monkeypatch):
    """Redis 'tamamen erisilemez' durumunu simule et (``r._conn`` bos)."""
    monkeypatch.setattr(redis_module.r, "_conn", None)


# ---------------------------------------------------------------------------
# 1. _is_due
# ---------------------------------------------------------------------------


def test_is_due_true_when_last_run_is_none():
    client = CronClient()
    job = Job(name="j", source="x", interval_ms=1000, last_run=None)
    assert client._is_due(job) is True


def test_is_due_false_before_interval_elapsed(monkeypatch):
    client = CronClient()
    last_run = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    job = Job(name="j", source="x", interval_ms=10_000, last_run=last_run)
    _freeze_now(monkeypatch, last_run + timedelta(seconds=5))
    assert client._is_due(job) is False


def test_is_due_true_exactly_at_interval_boundary(monkeypatch):
    """Sinir >= : elapsed == interval_ms oldugunda da vadesi gelmis sayilmali."""
    client = CronClient()
    last_run = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    job = Job(name="j", source="x", interval_ms=10_000, last_run=last_run)
    _freeze_now(monkeypatch, last_run + timedelta(seconds=10))
    assert client._is_due(job) is True


def test_is_due_true_after_interval_elapsed(monkeypatch):
    client = CronClient()
    last_run = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    job = Job(name="j", source="x", interval_ms=10_000, last_run=last_run)
    _freeze_now(monkeypatch, last_run + timedelta(seconds=11))
    assert client._is_due(job) is True


def test_is_due_false_one_millisecond_before_boundary(monkeypatch):
    client = CronClient()
    last_run = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    job = Job(name="j", source="x", interval_ms=10_000, last_run=last_run)
    _freeze_now(monkeypatch, last_run + timedelta(seconds=9, milliseconds=999))
    assert client._is_due(job) is False


# ---------------------------------------------------------------------------
# 2. _claim -- Redis kilidi
# ---------------------------------------------------------------------------


async def test_claim_acquires_lock_when_free_and_writes_expected_key(fake_redis, monkeypatch):
    _always_true_conn(monkeypatch)
    client = CronClient()
    result = await client._claim("job-x", 30_000)
    assert result is True
    assert fake_redis.store.get(f"{PREFIX}lock:job-x") == "1"


async def test_claim_fails_when_already_locked_in_redis(fake_redis, monkeypatch):
    """GERCEK HATA regresyonu: Redis saglikli ve kilit baskasindaysa is CALISMAMALI."""
    _always_true_conn(monkeypatch)
    fake_redis.store[f"{PREFIX}lock:job-x"] = "1"  # baska worker onceden almis
    client = CronClient()
    result = await client._claim("job-x", 30_000)
    assert result is False
    # Yerel fallback'e hic dusulmemeli -- Redis erisilebilirdi.
    assert client._fallback_locks == {}


async def test_claim_ttl_seconds_equals_interval_ms_over_1000(monkeypatch):
    captured = {}

    async def _spy_set(key, value, nx=False, ex=None, xx=False):
        captured.update(key=key, ex=ex, nx=nx, value=value)
        return True

    monkeypatch.setattr(cron_module.r, "set", _spy_set)
    client = CronClient()
    result = await client._claim("job-y", 45_000)
    assert result is True
    assert captured == {"key": f"{PREFIX}lock:job-y", "ex": 45, "nx": True, "value": "1"}


async def test_claim_ttl_has_minimum_of_one_second(monkeypatch):
    captured = {}

    async def _spy_set(key, value, nx=False, ex=None, xx=False):
        captured["ex"] = ex
        return True

    monkeypatch.setattr(cron_module.r, "set", _spy_set)
    client = CronClient()
    await client._claim("job-z", 500)  # 0.5s -> max(1, 0) = 1
    assert captured["ex"] == 1


async def test_claim_returns_false_when_real_redis_lock_is_held_by_another_worker(monkeypatch):
    """GERCEK HATA (duzeltildi): NX cakismasi != Redis erisilemez.

    redis-py'nin gercek davranisi (bkz. tests/test_cron_integration.py ile
    dogrulandi): ``set(nx=True)`` anahtar zaten varsa da None doner. Duzeltme
    oncesi bu durum "Redis erisilemez" ile ayni islenip yerel fallback
    kilide dusuluyor ve is yine de calistiriliyordu.
    """

    async def _nx_collision(key, value, nx=False, ex=None, xx=False):
        return None

    monkeypatch.setattr(cron_module.r, "set", _nx_collision)
    _always_true_conn(monkeypatch)

    client = CronClient()
    result = await client._claim("busy-job", 60_000)

    assert result is False
    assert client._fallback_locks == {}


async def test_claim_uses_local_fallback_only_when_redis_truly_unreachable(monkeypatch):
    async def _redis_down(key, value, nx=False, ex=None, xx=False):
        return None

    monkeypatch.setattr(cron_module.r, "set", _redis_down)
    _no_conn(monkeypatch)

    client = CronClient()
    first = await client._claim("degraded-job", 60_000)
    second = await client._claim("degraded-job", 60_000)

    assert first is True
    # Ayni surec icinde, kilit suresi dolmadan ikinci kez alinamamali.
    assert second is False


async def test_claim_fallback_lock_releases_after_ttl_elapses(monkeypatch):
    """Fallback kilit TTL'i gecince tekrar alinabilmeli (pasif 'birakma')."""

    async def _redis_down(key, value, nx=False, ex=None, xx=False):
        return None

    monkeypatch.setattr(cron_module.r, "set", _redis_down)
    _no_conn(monkeypatch)

    now = 1_000.0
    monkeypatch.setattr(cron_module.time, "monotonic", lambda: now)

    client = CronClient()
    first = await client._claim("degraded-job", 2_000)  # ttl=2s
    assert first is True

    now = 1_001.0  # 1s sonra -- TTL henuz dolmadi
    monkeypatch.setattr(cron_module.time, "monotonic", lambda: now)
    assert await client._claim("degraded-job", 2_000) is False

    now = 1_002.5  # TTL (2s) dolmus
    monkeypatch.setattr(cron_module.time, "monotonic", lambda: now)
    assert await client._claim("degraded-job", 2_000) is True


async def test_claim_fallback_prunes_stale_entries_when_over_threshold(monkeypatch):
    async def _redis_down(key, value, nx=False, ex=None, xx=False):
        return None

    monkeypatch.setattr(cron_module.r, "set", _redis_down)
    _no_conn(monkeypatch)

    now = 1_000_000.0
    monkeypatch.setattr(cron_module.time, "monotonic", lambda: now)

    client = CronClient()
    client._fallback_locks = {f"{PREFIX}lock:stale-{i}": now - 10 for i in range(10_001)}

    result = await client._claim("new-job", 1_000)

    assert result is True
    assert len(client._fallback_locks) == 1
    assert f"{PREFIX}lock:new-job" in client._fallback_locks


# ---------------------------------------------------------------------------
# 5. _run_code -- exec() sozlesmesi
# ---------------------------------------------------------------------------


async def test_run_code_awaits_cron_main_contract(probe):
    client = CronClient()
    source = (
        "import test_cron_probe as _p\n"
        "async def __cron_main__():\n"
        "    _p.calls.append('ran')\n"
    )
    code = client._compile(source, "t")
    await client._run_code(code, "t")
    assert probe.calls == ["ran"]


async def test_run_code_legacy_sync_format_runs_in_thread(probe, monkeypatch):
    """``__cron_main__`` tanimlamayan eski format thread'de exec edilir."""
    to_thread_calls = []
    orig_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, *args, **kwargs):
        to_thread_calls.append((func, args))
        return await orig_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(cron_module.asyncio, "to_thread", _spy_to_thread)

    client = CronClient()
    source = "import test_cron_probe as _p\n_p.calls.append('sync-ran')\n"
    code = client._compile(source, "t")
    await client._run_code(code, "t")

    assert probe.calls == ["sync-ran"]
    assert len(to_thread_calls) == 1
    assert to_thread_calls[0][0] is exec  # legacy dal exec'i thread'de calistirir


async def test_run_code_propagates_runtime_error_from_cron_main():
    """``_run_code`` kendisi istisnayi yutmaz -- yutma _run_job_worker'da olur."""
    client = CronClient()
    source = "async def __cron_main__():\n    raise ValueError('boom')\n"
    code = client._compile(source, "t")
    with pytest.raises(ValueError, match="boom"):
        await client._run_code(code, "t")


def test_compile_invalid_syntax_raises_immediately():
    client = CronClient()
    with pytest.raises(SyntaxError):
        client._compile("def broken(:\n", "bad")


async def test_register_job_with_syntax_error_does_not_reach_db(fake_db):
    """Bozuk kaynak kod register_job'da erkenden patlar, DB'ye hic gidilmez.

    Not: ``self._jobs[name] = job`` derlemeden ONCE atandigi icin ``get_job``
    yine de bir Job dondurur (kismi durum) ama ``_code_dict`` bos kalir ve
    hicbir DB yazisi yapilmaz -- bu mevcut (kucuk, zararsiz) davranis burada
    belgeleniyor.
    """
    client = CronClient()
    with pytest.raises(SyntaxError):
        await client.register_job("bad", 1000, "def broken(:\n")
    assert "bad" not in client._code_dict
    assert fake_db.queries == []


# ---------------------------------------------------------------------------
# register_job / get_job / list_jobs / remove_job / run_job
# ---------------------------------------------------------------------------

_OK_SOURCE = "async def __cron_main__():\n    pass\n"


async def test_register_job_persists_and_compiles(fake_db):
    client = CronClient()
    await client.register_job("j", 1000, _OK_SOURCE, "desc")
    assert client.get_job("j").name == "j"
    assert "j" in client._code_dict
    inserts = [q for q in fake_db.queries if "INSERT INTO cron_jobs" in q[0]]
    assert len(inserts) == 1


async def test_remove_job_clears_all_state_and_deletes_row(fake_db):
    client = CronClient()
    await client.register_job("j", 1000, _OK_SOURCE)
    client._running.add("j")
    await client.remove_job("j")
    assert client.get_job("j") is None
    assert "j" not in client._code_dict
    assert "j" not in client._running
    deletes = [q for q in fake_db.queries if "DELETE FROM cron_jobs" in q[0]]
    assert deletes[0][1] == ("j",)


async def test_list_jobs_sorted_by_name(fake_db):
    client = CronClient()
    for name in ("zeta", "alpha", "mid"):
        await client.register_job(name, 1000, _OK_SOURCE)
    assert [j.name for j in client.list_jobs()] == ["alpha", "mid", "zeta"]


async def test_run_job_returns_false_for_unknown_job():
    client = CronClient()
    assert await client.run_job("nope") is False


async def test_run_job_executes_registered_code(fake_db, probe):
    client = CronClient()
    source = "import test_cron_probe as _p\nasync def __cron_main__():\n    _p.calls.append('run_job')\n"
    await client.register_job("j", 1000, source)
    result = await client.run_job("j")
    assert result is True
    assert probe.calls == ["run_job"]


# ---------------------------------------------------------------------------
# 3 & 4. _running seti + hata yolu (_run_job_worker)
# ---------------------------------------------------------------------------


async def test_run_job_worker_exception_is_caught_last_run_updates_job_not_disabled(fake_db):
    client = CronClient()
    source = "async def __cron_main__():\n    raise RuntimeError('boom')\n"
    job = Job(name="j1", source=source, interval_ms=1000, last_run=None)
    client._jobs["j1"] = job
    client._code_dict["j1"] = client._compile(source, "j1")
    client._running.add("j1")

    await client._run_job_worker("j1")  # istisna disariya sizmamali

    # last_run guncellendi (DB UPDATE + bellekteki job nesnesi).
    updates = [q for q in fake_db.queries if "UPDATE cron_jobs SET last_run" in q[0]]
    assert len(updates) == 1
    assert updates[0][1][1] == "j1"
    assert job.last_run is not None
    # _running'den cikarildi.
    assert "j1" not in client._running
    # DB baglantisi havuza iade edildi.
    assert fake_db.release_calls == 1
    # is DEVRE DISI BIRAKILMADI -- hala kayitli.
    assert "j1" in client._jobs
    assert "j1" in client._code_dict


async def test_run_due_jobs_skips_job_already_in_running_set(monkeypatch, fake_db):
    client = CronClient()
    job = Job(name="j1", source=_OK_SOURCE, interval_ms=1000, last_run=None)
    client._jobs["j1"] = job
    client._code_dict["j1"] = client._compile(_OK_SOURCE, "j1")
    client._running.add("j1")  # is zaten calisiyor

    claim_calls = []

    async def _claim_spy(name, interval_ms):
        claim_calls.append(name)
        return True

    monkeypatch.setattr(client, "_claim", _claim_spy)

    created = []
    monkeypatch.setattr(
        cron_module.asyncio, "create_task", lambda coro, **kw: created.append(coro)
    )

    await client.run_due_jobs()

    assert claim_calls == []  # calisan is icin kilit bile denenmedi
    assert created == []


async def test_run_due_jobs_claims_and_schedules_when_not_running(monkeypatch, fake_db):
    client = CronClient()
    job = Job(name="j1", source=_OK_SOURCE, interval_ms=1000, last_run=None)
    client._jobs["j1"] = job
    client._code_dict["j1"] = client._compile(_OK_SOURCE, "j1")

    claimed = {}

    async def _claim_spy(name, interval_ms):
        claimed["args"] = (name, interval_ms)
        return True

    monkeypatch.setattr(client, "_claim", _claim_spy)

    created = []

    def _fake_create_task(coro, **kw):
        created.append(coro)

        class _FakeTask:
            def done(self):
                return True

        return _FakeTask()

    monkeypatch.setattr(cron_module.asyncio, "create_task", _fake_create_task)

    await client.run_due_jobs()

    assert claimed["args"] == ("j1", 1000)
    assert "j1" in client._running
    assert len(created) == 1
    created[0].close()  # calistirilmayacagi icin "never awaited" uyarisini onle


async def test_run_due_jobs_does_not_schedule_when_claim_fails(monkeypatch, fake_db):
    client = CronClient()
    job = Job(name="j1", source=_OK_SOURCE, interval_ms=1000, last_run=None)
    client._jobs["j1"] = job
    client._code_dict["j1"] = client._compile(_OK_SOURCE, "j1")

    async def _claim_false(name, interval_ms):
        return False

    monkeypatch.setattr(client, "_claim", _claim_false)

    created = []
    monkeypatch.setattr(
        cron_module.asyncio, "create_task", lambda coro, **kw: created.append(coro)
    )

    await client.run_due_jobs()

    assert created == []
    assert "j1" not in client._running


async def test_run_due_jobs_isolates_failures_between_jobs(monkeypatch, fake_db):
    """Bir is hata verse bile digeri etkilenmemeli -- ikisi de last_run alir."""
    client = CronClient()
    bad_source = "async def __cron_main__():\n    raise RuntimeError('boom')\n"
    for name, source in (("ok_job", _OK_SOURCE), ("bad_job", bad_source)):
        job = Job(name=name, source=source, interval_ms=1000, last_run=None)
        client._jobs[name] = job
        client._code_dict[name] = client._compile(source, name)

    async def _claim_always_true(name, interval_ms):
        return True

    client._claim = _claim_always_true

    created_tasks = []
    orig_create_task = asyncio.create_task

    def _tracking_create_task(coro, **kw):
        t = orig_create_task(coro, **kw)
        created_tasks.append(t)
        return t

    monkeypatch.setattr(cron_module.asyncio, "create_task", _tracking_create_task)

    await client.run_due_jobs()
    await asyncio.gather(*created_tasks)

    assert client._running == set()
    assert client._jobs["ok_job"].last_run is not None
    assert client._jobs["bad_job"].last_run is not None
    updates = [q for q in fake_db.queries if "UPDATE cron_jobs SET last_run" in q[0]]
    updated_names = {p[1] for _, p in updates}
    assert updated_names == {"ok_job", "bad_job"}


# ---------------------------------------------------------------------------
# 6. last_run YAZMA ZAMANLAMASI -- is bitmeden ONCE degil, bitince
# ---------------------------------------------------------------------------


async def test_last_run_is_written_only_after_job_completes(fake_db, monkeypatch):
    """Uzun suren bir is bir sonraki tick'i kaydirir -- last_run ancak
    is tamamlaninca yazilir, baslarken degil."""
    gate = asyncio.Event()
    probe_mod = types.ModuleType("test_cron_probe_gate")
    probe_mod.gate = gate
    monkeypatch.setitem(sys.modules, "test_cron_probe_gate", probe_mod)

    client = CronClient()
    source = (
        "import test_cron_probe_gate as _p\n"
        "async def __cron_main__():\n"
        "    await _p.gate.wait()\n"
    )
    job = Job(name="slow", source=source, interval_ms=1000, last_run=None)
    client._jobs["slow"] = job
    client._code_dict["slow"] = client._compile(source, "slow")

    task = asyncio.create_task(client._run_job_worker("slow"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # is'in gate.wait()'e ulasmasina firsat ver

    updates_before = [q for q in fake_db.queries if "UPDATE cron_jobs SET last_run" in q[0]]
    assert updates_before == []
    assert job.last_run is None

    gate.set()
    await task

    updates_after = [q for q in fake_db.queries if "UPDATE cron_jobs SET last_run" in q[0]]
    assert len(updates_after) == 1
    assert job.last_run is not None


# ---------------------------------------------------------------------------
# ON CONFLICT DO UPDATE -- last_run KORUNMALI (item 7, cron.py tarafi)
# ---------------------------------------------------------------------------


async def test_save_to_db_sql_never_overwrites_last_run_on_conflict(fake_db):
    """Regresyon koruyucu: DO UPDATE govdesi last_run icermemeli.

    Icerirse her restart'ta tum isler ON CONFLICT yolundan last_run'i
    ezip yeniden tetiklenirdi (bkz. src/cron/register.py docstring'i).
    """
    client = CronClient()
    job = Job(name="j", source=_OK_SOURCE, interval_ms=1000, last_run=None)
    await client._save_to_db(job)

    inserts = [q for q in fake_db.queries if "INSERT INTO cron_jobs" in q[0]]
    assert len(inserts) == 1
    sql = inserts[0][0]
    assert "ON CONFLICT (name) DO UPDATE SET" in sql
    set_clause = sql.split("DO UPDATE SET", 1)[1]
    assert "last_run" not in set_clause
    assert "description = EXCLUDED.description" in set_clause
    assert "source = EXCLUDED.source" in set_clause
    assert "interval_ms = EXCLUDED.interval_ms" in set_clause


# ---------------------------------------------------------------------------
# init() / start() / stop()
# ---------------------------------------------------------------------------


async def test_init_loads_jobs_from_db_and_releases_connection(fake_db):
    client = CronClient()
    fake_db.fetchall_result = [("job_a", "desc", _OK_SOURCE, 1000, None)]
    await client.init()
    assert client._initialized is True
    assert client.get_job("job_a") is not None
    assert "job_a" in client._code_dict
    assert fake_db.release_calls == 1


async def test_init_is_idempotent_second_call_noop(fake_db):
    client = CronClient()
    fake_db.fetchall_result = [("job_a", "desc", _OK_SOURCE, 1000, None)]
    await client.init()
    fake_db.queries.clear()
    await client.init()
    assert fake_db.queries == []  # ikinci cagri DB'ye hic gitmedi


async def test_init_releases_connection_even_when_loading_fails(fake_db, monkeypatch):
    client = CronClient()

    def _boom(row_factory=None, **kw):
        raise RuntimeError("db down")

    # Not: fake_db'nin KENDI ozniteligini degil, db_module.db.cursor'a
    # ONCEDEN baglanmis referansi degistirmemiz gerekiyor -- fixture
    # ``fdb.cursor``'i tek seferlik bound-method olarak baglar.
    monkeypatch.setattr(cron_module.db, "cursor", _boom)

    with pytest.raises(RuntimeError):
        await client.init()

    assert fake_db.release_calls == 1
    assert client._initialized is False


async def test_start_and_stop_lifecycle(fake_db):
    client = CronClient()
    client._check_interval = 0  # gercek 1s sleep'i teste sizdirma
    fake_db.fetchall_result = []

    await client.start()
    assert client._worker_task is not None
    assert not client._worker_task.done()

    await client.stop()
    assert client._worker_task is None
