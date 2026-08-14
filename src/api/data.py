"""Eski yillik gunluk veri rotasi — KULLANIM DISI.

``GET /api/v1/data/daily/{year}`` 410 Gone doner: veri disa aktarim
Google Takeout tarzina tasindi (``src/api/exports.py`` + arka plan iscisi
``src/services/export_jobs.py``). On-demand fill mantigi da tasindi:
``src/services/bulk.py`` (``fill_year`` / ``build_candle_rows_bulk``).
"""

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/data/daily/{year}")
async def daily_data(year: int):
    raise HTTPException(
        status_code=410,
        detail="Kullanım dışı — POST /api/v1/data/export ile istek oluşturun",
    )
