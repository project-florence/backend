"""Read helpers for the market digest feature (async, down-tolerant).

Every helper never raises: Redis is unavailable (``r`` returns None) and DB
errors are swallowed, yielding ``None`` or an empty list so the read API stays
degraded-but-alive. Slot window resolution uses the digest config's
``slot_times`` (HH:MM) in the configured timezone (Europe/Istanbul).
"""

import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.core.config import get_config
from src.core.database import db
from src.core.redis import r
from src.services.digest.models import Digest, DigestSection

logger = logging.getLogger(__name__)

_DIGEST_COLUMNS = "id, date, slot, title, content, sections, metadata, language, created_at"


def _digest_tz() -> ZoneInfo:
    return ZoneInfo(get_config()["digest"]["timezone"])


def _slot_for_datetime(at: datetime) -> str:
    """Map a datetime to the digest slot whose window covers it.

    Rule (slot_times boundaries, earliest slot wins before the first slot):
    a slot covers from its slot time until the next slot's time; before the
    first slot (morning) everything still belongs to morning. So for
    ``slot_times = {morning: 09:45, noon: 13:15, evening: 18:45}``:
    ``[00:00, 13:15) -> morning``, ``[13:15, 18:45) -> noon``,
    ``[18:45, 24:00) -> evening``.
    """
    slot_times = get_config()["digest"]["slot_times"]
    ordered = sorted(slot_times.items(), key=lambda kv: kv[1])
    local = at.astimezone(_digest_tz()) if at.tzinfo else at.replace(tzinfo=_digest_tz())
    hhmm = local.strftime("%H:%M")
    chosen = ordered[0][0]
    for slot, slot_hhmm in ordered:
        if hhmm >= slot_hhmm:
            chosen = slot
    return chosen


def _digest_from_row(row) -> Digest | None:
    """Deserialize a dict row into a Digest (down-tolerant)."""
    if row is None:
        return None
    try:
        sections = row.get("sections") or []
        if isinstance(sections, str):
            sections = json.loads(sections)
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return Digest(
            id=row["id"],
            date=row["date"],
            slot=row["slot"],
            title=row["title"],
            content=row.get("content"),
            sections=[DigestSection(**s) for s in sections],
            metadata=metadata,
            language=row.get("language") or "tr",
            created_at=row["created_at"],
        )
    except Exception as e:
        logger.warning("Failed to deserialize digest row: %s", e)
        return None


async def get_current_digest() -> Digest | None:
    """Read the current digest from Redis (config key), None if missing/invalid."""
    redis_key = get_config()["digest"]["redis_key"]
    try:
        raw = await r.get(redis_key)
    except Exception as e:
        logger.warning("Failed to read current digest from Redis: %s", e)
        return None
    if not raw:
        return None
    try:
        return Digest(**json.loads(raw))
    except Exception as e:
        logger.warning("Invalid current digest payload in Redis: %s", e)
        return None


async def get_digest_by_date_slot(digest_date: date, slot: str) -> Digest | None:
    """Return the digest for a (date, slot) from the digests table, or None."""
    try:
        async with db.cursor() as cur:
            await cur.execute(
                f"SELECT {_DIGEST_COLUMNS} FROM digests "
                "WHERE date = %s AND slot = %s ORDER BY created_at DESC LIMIT 1",
                (digest_date, slot),
            )
            row = await cur.fetchone()
    except Exception as e:
        logger.warning("Failed to read digest (date=%s, slot=%s): %s", digest_date, slot, e)
        return None
    return _digest_from_row(row)


async def get_digests_by_date(digest_date: date) -> list[Digest]:
    """Return all digests for a date ordered by slot (morning, noon, evening)."""
    try:
        async with db.cursor() as cur:
            await cur.execute(
                f"SELECT {_DIGEST_COLUMNS} FROM digests WHERE date = %s",
                (digest_date,),
            )
            rows = await cur.fetchall()
    except Exception as e:
        logger.warning("Failed to read digests (date=%s): %s", digest_date, e)
        return []

    slot_times = get_config()["digest"]["slot_times"]
    slot_order = {slot: i for i, slot in enumerate(slot_times)}
    digests = [d for d in (_digest_from_row(row) for row in rows) if d is not None]
    digests.sort(key=lambda d: slot_order.get(d.slot, len(slot_order)))
    return digests


async def get_digest_at(at: datetime) -> Digest | None:
    """Return the digest whose slot window covers the given datetime.

    The datetime is resolved to its Istanbul date and slot (see
    ``_slot_for_datetime``), then the (date, slot) row is returned from the
    digests table. Returns None when no such digest exists yet.
    """
    local = at.astimezone(_digest_tz()) if at.tzinfo else at.replace(tzinfo=_digest_tz())
    slot = _slot_for_datetime(local)
    return await get_digest_by_date_slot(local.date(), slot)
