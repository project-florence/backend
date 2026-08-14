import asyncio
import contextvars
import json
import logging

from src.core.database import db

logger = logging.getLogger(__name__)

# Arka plan task'larini canli tut: referanssiz task'lar GC tarafindan
# silinebilir (task kaybi). done_callback ile set'ten cikar + hata loglar.
_tasks: set[asyncio.Task] = set()


def _on_task_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task failed: %s", exc)


async def track_event(event_type: str, user_id: int | None = None, ticker: str | None = None, details: dict | None = None):
    """Analitik olayini kaydeder. Hata durumunda sessizce gecerr (yalnizca log)."""
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute("""
                INSERT INTO analytics_events (event_type, user_id, ticker, details)
                VALUES (%s, %s, %s, %s)
            """, (event_type, user_id, ticker, json.dumps(details or {})))
            await db.commit()
    except Exception as e:
        logger.warning("Analytics track_event failed: %s", e)


def fire_and_forget(event_type: str, user_id: int | None = None, ticker: str | None = None, details: dict | None = None):
    """Eski thread-tabanli davranis: olayi arka planda task olarak kaydeder.

    Yalnizca calisan bir event loop icinde cagrilmalidir (request/middleware).
    Task, modul seviyesindeki set'te tutulur (GC'ye kaptirilmaz); tamamlaninca
    set'ten cikar, hata varsa loglanir.
    """
    try:
        # BOS context: request'in ContextVar baglantisini (db._current_conn)
        # miras alma. Boylece arka plan task'i kendi baglantisini havuzdan
        # alir; request bitip baglanti iade edilse bile task bagimsiz calisir.
        task = asyncio.create_task(
            track_event(event_type, user_id, ticker, details),
            context=contextvars.Context(),
        )
    except RuntimeError as e:
        # Calisan event loop yok (ornegin shutdown sirasinda): sessiz yutma,
        # logla.
        logger.warning("fire_and_forget skipped (no running event loop): %s", e)
        return
    _tasks.add(task)
    task.add_done_callback(_on_task_done)
