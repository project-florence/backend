"""Cron islerini CronClient'a kaydeder.

Her baslatmada cagrilir; `register_job` ON CONFLICT DO UPDATE ile DB'deki
kaynak kodunu kod ile senkron tutar. Artik kodda olmayan isler temizlenir.
"""

import logging

from src.clients.cron import cron_client

logger = logging.getLogger(__name__)


def _job_specs() -> list[tuple[str, int, str, str]]:
    return [
        (
            "price_bist30",
            10 * 60 * 1000,
            "from src.cron.tasks import run_update_bist30\nrun_update_bist30()",
            "BIST30 fiyat guncellemesi (10 dk)",
        ),
        (
            "price_popular",
            60 * 60 * 1000,
            "from src.cron.tasks import run_update_popular\nrun_update_popular()",
            "Populer fiyat + profil guncellemesi (saatlik)",
        ),
        (
            "price_rest",
            12 * 60 * 60 * 1000,
            "from src.cron.tasks import run_update_rest\nrun_update_rest()",
            "Kalan hisseler fiyat guncellemesi (12 saat)",
        ),
        (
            "credit_refill",
            24 * 60 * 60 * 1000,
            "from src.cron.tasks import run_credit_refill\nrun_credit_refill()",
            "Gunluk free kredi dolumu",
        ),
        (
            "seed_vectors",
            24 * 60 * 60 * 1000,
            "from src.cron.tasks import run_seed_vectors\nrun_seed_vectors()",
            "Gunluk stock vector hesaplama",
        ),
        (
            "cleanup_old_data",
            24 * 60 * 60 * 1000,
            "from src.cron.tasks import run_cleanup_old_data\nrun_cleanup_old_data()",
            "Eski mum verisi temizligi",
        ),
        (
            "warm_price_cache",
            24 * 60 * 60 * 1000,
            "from src.cron.tasks import run_warm_price_cache\nrun_warm_price_cache()",
            "Redis fiyat cache on-isitma",
        ),
    ]


def register_cron_jobs() -> None:
    specs = _job_specs()
    desired = {name for name, *_ in specs}

    for name, interval_ms, snippet, description in specs:
        cron_client.register_job(name, interval_ms, snippet, description)

    for existing in cron_client.list_jobs():
        if existing.name not in desired:
            logger.info("Cron job '%s' artik kayitli degil, kaldiriliyor", existing.name)
            cron_client.remove_job(existing.name)
