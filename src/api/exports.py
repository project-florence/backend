"""Veri disa aktarim API'si (Google Takeout tarzi).

- ``POST /api/v1/data/export`` — {year, format} -> 202 {export_id, status},
  arka planda isci (``export_jobs._run_export``) hazirlar, e-posta ile
  indirme linki gider. Ayni kullanici + year + format icin aktif kayit
  varsa mevcut id donulur (idempotent). Rate limit: 3 istek / 3600 sn.
- ``GET /api/v1/data/export`` — kullanicinin kendi export listesi.
- ``GET /api/v1/data/export/{export_id}`` — tek kayit (sahibi degilse 404).
- ``GET /api/v1/data/export/download/{token}`` — PUBLIC: token ile indirme
  (status ready/sent + expires_at gelecekte; degilse 410).
"""

import asyncio
import contextvars
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.api.deps import get_current_user
from src.core.database import db
from src.core.ratelimit import rate_limiter
from src.services.export_jobs import _run_export

logger = logging.getLogger(__name__)

router = APIRouter()

# Arka plan export task'lari: referanssiz task'lar GC tarafindan silinebilir;
# set'te tutulur, done_callback ile cikarilir + hata loglanir (analytics.py
# deseni).
_tasks: set[asyncio.Task] = set()


class ExportRequest(BaseModel):
    year: int = Field(..., description="Veri yili (1990..simdiki yil+1)")
    format: Literal["csv", "json"] = "csv"


def _on_task_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Export gorevi hata verdi: %s", exc)


def _start_export_task(export_id: int) -> None:
    """Export iscisini arka planda baslatir.

    BOS context: request'in ContextVar baglantisini (db._current_conn)
    miras alma. Boylece isci kendi baglantisini havuzdan alir; request
    bitip baglanti iade edilse bile task bagimsiz calisir.
    """
    try:
        task = asyncio.create_task(_run_export(export_id), context=contextvars.Context())
    except RuntimeError as e:
        # Calisan event loop yok (shutdown sirasinda): kayit queued kalir,
        # sonraki istekler ayni kaydi tekrar kuyruga almaz (idempotent).
        logger.warning("Export task baslatilamadi (export_id=%s): %s", export_id, e)
        return
    _tasks.add(task)
    task.add_done_callback(_on_task_done)


def _serialize(row: tuple) -> dict:
    """exports satirini (id, user_id, ...) API yanitina cevirir."""
    (
        _id, _user_id, year, fmt, status, file_path, token,
        expires_at, row_count, size_bytes, downloaded_count, error, created_at, updated_at,
    ) = row
    now = datetime.now(timezone.utc)
    return {
        "id": _id,
        "year": year,
        "format": fmt,
        "status": status,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "row_count": row_count,
        "size_bytes": size_bytes,
        "downloaded_count": downloaded_count,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "error": error,
        "downloadable": status in ("ready", "sent") and expires_at is not None and expires_at > now,
    }


@router.post("/data/export", status_code=202)
async def create_export(
    body: ExportRequest,
    current_user_id: int = Depends(get_current_user),
):
    await rate_limiter.check(f"export:{current_user_id}", max_requests=3, window_seconds=3600)

    now = datetime.now(timezone.utc)
    if body.year < 1990 or body.year > now.year + 1:
        raise HTTPException(status_code=400, detail="Invalid year")

    async with db.cursor(row_factory=None) as cur:
        # Idempotent: ayni kullanici + year + format icin aktif kayit varsa
        # mevcut id'yi don (yeni isci baslatilmaz).
        await cur.execute(
            "SELECT id, status FROM exports "
            "WHERE user_id = %s AND year = %s AND format = %s "
            "AND status IN ('queued','processing','ready','sent') "
            "ORDER BY id DESC LIMIT 1",
            (current_user_id, body.year, body.format),
        )
        existing = await cur.fetchone()
        if existing:
            await db.commit()
            return {"export_id": existing[0], "status": existing[1]}

        await cur.execute(
            "INSERT INTO exports (user_id, year, format, status) "
            "VALUES (%s, %s, %s, 'queued') RETURNING id",
            (current_user_id, body.year, body.format),
        )
        insert_row = await cur.fetchone()
        export_id = insert_row[0] if insert_row else None
        await db.commit()

    if export_id is None:
        raise HTTPException(status_code=500, detail="Export kaydi olusturulamadi")

    _start_export_task(export_id)
    return {"export_id": export_id, "status": "queued"}


@router.get("/data/export")
async def list_exports(current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT id, user_id, year, format, status, file_path, token, expires_at, "
            "row_count, size_bytes, downloaded_count, error, created_at, updated_at "
            "FROM exports WHERE user_id = %s ORDER BY created_at DESC",
            (current_user_id,),
        )
        rows = await cur.fetchall()
    return [_serialize(row) for row in rows]


@router.get("/data/export/download/{token}")
async def download_export(token: str):
    """PUBLIC indirme: token yeterli, auth yok."""
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT id, year, format, status, expires_at, file_path FROM exports WHERE token = %s",
            (token,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Export not found")

        _id, year, fmt, status, expires_at, file_path = row
        if status not in ("ready", "sent") or expires_at is None or expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="Export link expired or not ready")

        await cur.execute(
            "UPDATE exports SET downloaded_count = downloaded_count + 1 WHERE id = %s",
            (_id,),
        )
        # Commit cursor blogu icinde: blok cikisinda otomatik iade artisi
        # rollback etmesin.
        await db.commit()

    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="Export file missing")

    filename = f"florence-daily-{year}.{fmt}.gz"
    return FileResponse(
        file_path,
        media_type="application/gzip",
        filename=filename,
    )


@router.get("/data/export/{export_id}")
async def get_export(export_id: int, current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT id, user_id, year, format, status, file_path, token, expires_at, "
            "row_count, size_bytes, downloaded_count, error, created_at, updated_at "
            "FROM exports WHERE id = %s",
            (export_id,),
        )
        row = await cur.fetchone()
    if row is None or row[1] != current_user_id:
        raise HTTPException(status_code=404, detail="Export not found")
    return _serialize(row)
