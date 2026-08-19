"""Market digest read API (authenticated).

GET /api/v1/digest returns a generated daily market summary. Resolution
order when multiple selectors are provided (most specific wins):

1. ``date`` + ``slot``  -> the digest for that exact (date, slot), 404 if absent.
2. ``date`` only        -> JSON array of that date's digests (possibly empty).
3. ``at`` only          -> the digest whose slot window covers the ISO8601
   datetime (Istanbul slot boundaries), 404 if none exists.
4. none                 -> the current digest from Redis, 404 if unavailable.

Providing ``slot`` without ``date`` is invalid (422). Invalid ``date``,
``slot`` or ``at`` formats are rejected with 422. This endpoint is
authenticated (``get_current_user``) and intentionally NOT in PUBLIC_PATHS.
"""

import logging
from datetime import datetime
from datetime import date as date_type
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_current_user
from src.services.digest import reads

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/digest")
async def read_digest(
    current_user_id: int = Depends(get_current_user),
    date: date_type | None = Query(None, description="YYYY-MM-DD digest date"),
    slot: Literal["morning", "noon", "evening"] | None = Query(
        None, description="digest slot (requires date)"
    ),
    at: datetime | None = Query(None, description="ISO8601 datetime; returns the digest whose slot window covers it"),
):
    """Read a market digest (see module docstring for the resolution rules)."""
    if slot is not None and date is None:
        raise HTTPException(status_code=422, detail="slot requires date")

    if date is not None and slot is not None:
        digest = await reads.get_digest_by_date_slot(date, slot)
        if digest is None:
            raise HTTPException(status_code=404, detail="Digest not found")
        logger.info("[DIGEST] served by date+slot: %s %s", date, slot)
        return digest.model_dump(mode="json")

    if date is not None:
        digests = await reads.get_digests_by_date(date)
        logger.info("[DIGEST] served %d digest(s) for %s", len(digests), date)
        return [d.model_dump(mode="json") for d in digests]

    if at is not None:
        digest = await reads.get_digest_at(at)
        if digest is None:
            raise HTTPException(status_code=404, detail="Digest not found")
        logger.info("[DIGEST] served by at: %s", at.isoformat())
        return digest.model_dump(mode="json")

    digest = await reads.get_current_digest()
    if digest is None:
        raise HTTPException(status_code=404, detail="No digest available")
    logger.info("[DIGEST] served current digest: %s", digest.id)
    return digest.model_dump(mode="json")
