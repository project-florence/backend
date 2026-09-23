"""``src/cron/register.py`` icin hermetik testler.

TEST_COVERAGE_PLAN.md Adim C. Kapsar:

7. ``register_cron_jobs``'un ``ON CONFLICT (name) DO UPDATE`` davranisi:
   var olan bir isin ``last_run``'i KORUNUR (sifirlanmaz -- sifirlansa her
   restart'ta tum isler yeniden tetiklenirdi), yeni bir is ise
   ``_initial_last_run`` ile hesaplanmis bir baslangic degeri alir.
8. Kayitli her isin dispatcher govdesinin beklenen sekle uydugu
   (``from src.cron.tasks import X`` + ``async def __cron_main__(): await X()``).

``cron_client`` surec-omurlu bir singleton oldugundan (import zamaninda
olusturuluyor), testler her seferinde TAZE bir ``CronClient()`` orneği
kurup ``register_module.cron_client``'i ona yonlendirir -- testler arasi
durum sizintisi olmaz.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest

from src.clients.cron import CronClient, Job
from src.cron import register as register_module
from src.cron.register import (
    DAILY_CLOSE_HOUR,
    DAILY_CLOSE_MINUTE,
    MARKET_TIMEZONE,
    RATE_ANALYSIS_HOUR,
    RATE_ANALYSIS_MINUTE,
    RETENTION_HOUR,
    RETENTION_MINUTE,
    _initial_last_run,
    _job_specs,
    register_cron_jobs,
)

_OK_SOURCE = "async def __cron_main__():\n    pass\n"


class _FixedDatetime(datetime):
    _fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def _freeze_now(monkeypatch, when: datetime) -> None:
    _FixedDatetime._fixed = when
    monkeypatch.setattr(register_module, "datetime", _FixedDatetime)


@pytest.fixture
def fresh_cron_client(monkeypatch):
    """``register_module.cron_client``'i her testte taze bir orneğe yonlendirir."""
    client = CronClient()
    monkeypatch.setattr(register_module, "cron_client", client)
    return client


# ---------------------------------------------------------------------------
# 8. Dispatcher govde sekli -- her spec icin
# ---------------------------------------------------------------------------

_DISPATCHER_RE = re.compile(
    r"^from src\.cron\.tasks import (\w+)\n"
    r"async def __cron_main__\(\):\n"
    r"    await \1\(\)$"
)


def test_job_specs_dispatcher_bodies_match_expected_shape():
    specs = _job_specs()
    assert len(specs) > 0
    names = [name for name, *_ in specs]
    assert len(names) == len(set(names)), "is isimleri benzersiz olmali"

    for name, interval_ms, snippet, description in specs:
        match = _DISPATCHER_RE.match(snippet)
        assert match, f"{name}: dispatcher govdesi beklenen sekle uymuyor:\n{snippet!r}"
        # __cron_main__ tam olarak import edilen fonksiyonu cagirmali (ayni isim).
        imported_fn = match.group(1)
        assert f"import {imported_fn}" in snippet
        assert interval_ms > 0
        assert description  # bos aciklama olmamali


def test_job_specs_include_daily_close_repair_30_min():
    specs = {name: (interval_ms, description) for name, interval_ms, _snippet, description in _job_specs()}
    assert "daily_close_repair" in specs
    interval_ms, description = specs["daily_close_repair"]
    assert interval_ms == 30 * 60 * 1000
    assert description


def test_job_specs_compile_cleanly():
    """Her spec'in kaynak kodu register_job'un derleme adimindan gecebilmeli."""
    client = CronClient()
    for name, _interval_ms, snippet, _description in _job_specs():
        client._compile(snippet, name)  # SyntaxError firlatmamali


# ---------------------------------------------------------------------------
# 7. ON CONFLICT DO UPDATE -- last_run korunuyor / yeni is icin hesaplaniyor
# ---------------------------------------------------------------------------


async def test_register_cron_jobs_registers_every_spec(fresh_cron_client, fake_db):
    await register_cron_jobs()
    registered = {j.name for j in fresh_cron_client.list_jobs()}
    expected = {name for name, *_ in _job_specs()}
    assert registered == expected


async def test_register_preserves_existing_last_run(fresh_cron_client, fake_db):
    fixed_last_run = datetime(2026, 1, 1, tzinfo=UTC)
    await fresh_cron_client.register_job(
        "price_bist30", 600_000, _OK_SOURCE, "d", last_run=fixed_last_run
    )

    await register_cron_jobs()

    job = fresh_cron_client.get_job("price_bist30")
    assert job.last_run == fixed_last_run


async def test_register_new_job_gets_computed_initial_last_run(fresh_cron_client, fake_db, monkeypatch):
    fixed_now = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    expected = _initial_last_run("credit_refill", 24 * 60 * 60 * 1000)

    await register_cron_jobs()

    job = fresh_cron_client.get_job("credit_refill")
    assert job.last_run == expected
    assert job.last_run is not None


async def test_register_removes_jobs_no_longer_in_spec(fresh_cron_client, fake_db):
    await fresh_cron_client.register_job("ghost_job", 1000, _OK_SOURCE, "temp")
    assert fresh_cron_client.get_job("ghost_job") is not None

    await register_cron_jobs()

    assert fresh_cron_client.get_job("ghost_job") is None
    deletes = [q for q in fake_db.queries if "DELETE FROM cron_jobs" in q[0]]
    assert len(deletes) == 1
    assert deletes[0][1] == ("ghost_job",)


async def test_register_is_idempotent_across_two_calls(fresh_cron_client, fake_db):
    await register_cron_jobs()
    first_last_runs = {j.name: j.last_run for j in fresh_cron_client.list_jobs()}

    await register_cron_jobs()
    second_last_runs = {j.name: j.last_run for j in fresh_cron_client.list_jobs()}

    # Ikinci cagri hicbir last_run'i degistirmemeli (spec'te last_run yok,
    # sadece register_cron_jobs'un okudugu mevcut degeri geri yaziyor).
    assert first_last_runs == second_last_runs


# ---------------------------------------------------------------------------
# _initial_last_run -- ozel gunler
# ---------------------------------------------------------------------------


def test_initial_last_run_daily_close_targets_18_35_trt(monkeypatch):
    # Yerel saat hedeften ONCE -- ayni gun 18:35 TRT'ye offsetlenmeli.
    fixed_now = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)  # ~09:00 TRT
    _freeze_now(monkeypatch, fixed_now)

    result = _initial_last_run("daily_close", 24 * 60 * 60 * 1000)

    local_target = result.astimezone(MARKET_TIMEZONE) + timedelta(hours=24)
    assert local_target.hour == DAILY_CLOSE_HOUR
    assert local_target.minute == DAILY_CLOSE_MINUTE


def test_initial_last_run_rate_analysis_after_daily_close(monkeypatch):
    fixed_now = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    result = _initial_last_run("rate_analysis_daily", 24 * 60 * 60 * 1000)

    local_target = result.astimezone(MARKET_TIMEZONE) + timedelta(hours=24)
    assert local_target.hour == RATE_ANALYSIS_HOUR
    assert local_target.minute == RATE_ANALYSIS_MINUTE


def test_initial_last_run_retention_cleanup_targets_next_sunday_3am(monkeypatch):
    # 2026-08-27 bir Persembe (weekday()==3).
    fixed_now = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    result = _initial_last_run("retention_cleanup", 7 * 24 * 60 * 60 * 1000)

    local_target = result.astimezone(MARKET_TIMEZONE) + timedelta(days=7)
    assert local_target.weekday() == 6  # Pazar
    assert local_target.hour == RETENTION_HOUR
    assert local_target.minute == RETENTION_MINUTE


def test_initial_last_run_market_digest_is_config_driven_not_hardcoded(monkeypatch):
    """Slot saatleri src.core.config'den okunmali -- burada da hardcode YOK.

    Uretim kodundaki hesabi TAM olarak taklit ediyoruz ama girdiyi
    (``slot_times``) config'den okuyarak: config degisirse bu test de
    otomatik olarak dogru degeri bekler, sabit "09:30" gibi bir deger
    gomulmez.
    """
    from src.core.config import get_config

    fixed_now = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)  # erken TRT saati
    _freeze_now(monkeypatch, fixed_now)

    interval_ms = 10 * 60 * 1000
    slot_times = get_config()["digest"]["slot_times"]
    local = fixed_now.astimezone(MARKET_TIMEZONE)
    starts = []
    for hhmm in slot_times.values():
        hour, minute = map(int, hhmm.split(":"))
        start = local.replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(minutes=15)
        if start < local:
            start += timedelta(days=1)
        starts.append(start)
    expected_target = min(starts)
    expected = (expected_target - timedelta(seconds=interval_ms / 1000)).astimezone(UTC)

    result = _initial_last_run("market_digest", interval_ms)

    assert result == expected


def test_initial_last_run_generic_short_interval_tiers():
    # interval_ms <= 10dk -> now - (interval - 60s)
    interval_ms = 10 * 60 * 1000
    # datetime.now() gercek zamani kullanir; sadece deger araligini dogrula.
    result = _initial_last_run("some_new_10min_job", interval_ms)
    delta = datetime.now(UTC) - result
    # now - (600 - 60) = now - 540s; birkac saniyelik yurutme payi birak.
    assert timedelta(seconds=535) <= delta <= timedelta(seconds=545)
