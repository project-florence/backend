import asyncio
import json
import logging

from src.core.database import db

logger = logging.getLogger(__name__)


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
    """
    try:
        asyncio.create_task(track_event(event_type, user_id, ticker, details))
    except RuntimeError:
        pass
