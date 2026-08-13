"""Cron islerini CronClient'a kaydeder.

Her baslatmada cagrilir; `register_job` ON CONFLICT DO UPDATE ile DB'deki
kaynak kodunu kod ile senkron tutar. Artik kodda olmayan isler temizlenir.

Gorev kaynak kodlari `async def __cron_main__()` tanimlar; CronClient bu
fonksiyonu await ederek calistirir (bkz. src/clients/cron.py).

Ilk kurulumda isler ayni anda (burst) calismasin diye her ise farkli bir
ilk offset verilir; sonraki baslatmalarda DB'deki `last_run` korunur.
"""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.clients.cron import cron_client

logger = logging.getLogger(__name__)

MARKET_TIMEZONE = ZoneInfo("Europe/Istanbul")
DAILY_CLOSE_HOUR = 18
DAILY_CLOSE_MINUTE = 35


def _job_specs() -> list[tuple[str, int, str, str]]:
    return [
        (
            "price_bist30",
            10 * 60 * 1000,
            "from src.cron.tasks import run_update_bist30\n"
            "async def __cron_main__():\n"
            "    await run_update_bist30()",
            "BIST30 fiyat guncellemesi (10 dk)",
        ),
        (
            "price_popular",
            60 * 60 * 1000,
            "from src.cron.tasks import run_update_popular\n"
            "async def __cron_main__():\n"
            "    await run_update_popular()",
            "Populer fiyat + profil guncellemesi (saatlik)",
        ),
        (
            "price_rest",
            12 * 60 * 60 * 1000,
            "from src.cron.tasks import run_update_rest\n"
            "async def __cron_main__():\n"
            "    await run_update_rest()",
            "Kalan hisseler fiyat guncellemesi (12 saat)",
        ),
        (
            "credit_refill",
            24 * 60 * 60 * 1000,
            "from src.cron.tasks import run_credit_refill\n"
            "async def __cron_main__():\n"
            "    await run_credit_refill()",
            "Gunluk free kredi dolumu",
        ),
        (
            "seed_vectors",
            24 * 60 * 60 * 1000,
            "from src.cron.tasks import run_seed_vectors\n"
            "async def __cron_main__():\n"
            "    await run_seed_vectors()",
            "Gunluk stock vector hesaplama",
        ),
        (
            "warm_price_cache",
            24 * 60 * 60 * 1000,
            "from src.cron.tasks import run_warm_price_cache\n"
            "async def __cron_main__():\n"
            "    await run_warm_price_cache()",
            "Redis fiyat cache on-isitma",
        ),
        (
            "daily_close",
            24 * 60 * 60 * 1000,
            "from src.cron.tasks import run_update_daily_closes\n"
            "async def __cron_main__():\n"
            "    await run_update_daily_closes()",
            "Gunluk kapanis mumlari (18:35 TRT)",
        ),
    ]


def _initial_last_run(name: str, interval_ms: int) -> datetime:
    """Ilk kurulumda bir isin ilk ne zaman calisacagini belirler."""
    now = datetime.now(UTC)
    interval_s = interval_ms / 1000.0

    if name == "daily_close":
        local = now.astimezone(MARKET_TIMEZONE)
        target = local.replace(hour=DAILY_CLOSE_HOUR, minute=DAILY_CLOSE_MINUTE, second=0, microsecond=0)
        if local >= target:
            target = target + timedelta(days=1)
        return (target - timedelta(hours=24)).astimezone(UTC)

    if interval_ms <= 10 * 60 * 1000:
        return now - timedelta(seconds=interval_s - 60)
    if interval_ms <= 60 * 60 * 1000:
        return now - timedelta(seconds=interval_s - 15 * 60)
    if interval_ms <= 12 * 60 * 60 * 1000:
        return now - timedelta(seconds=interval_s - 60 * 60)

    daily_offsets_hours = {
        "credit_refill": 1,
        "seed_vectors": 2,
        "warm_price_cache": 6,
    }
    offset_hours = daily_offsets_hours.get(name, 2)
    return now - timedelta(hours=24 - offset_hours)


async def register_cron_jobs() -> None:
    specs = _job_specs()
    desired = {name for name, *_ in specs}

    existing_last_run = {job.name: job.last_run for job in cron_client.list_jobs()}

    for name, interval_ms, snippet, description in specs:
        last_run = existing_last_run.get(name) or _initial_last_run(name, interval_ms)
        await cron_client.register_job(name, interval_ms, snippet, description, last_run=last_run)

    for existing in cron_client.list_jobs():
        if existing.name not in desired:
            logger.info("Cron job '%s' artik kayitli degil, kaldiriliyor", existing.name)
            await cron_client.remove_job(existing.name)
