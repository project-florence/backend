import json
from fastapi import APIRouter, Query, HTTPException
from src.services.ipo import get_upcoming_ipos, get_draft_ipos, get_active_ipos, get_ipo_detail_by_slug

router = APIRouter()


@router.get("/ipos/upcoming")
def ipos_upcoming(after: str | None = Query(default=None, description="ISO date filter (e.g. 2026-06-01T00:00:00). Defaults to last 30 days.")):
    return json.loads(get_upcoming_ipos(after=after))


@router.get("/ipos/draft")
def ipos_draft(after: str | None = Query(default=None, description="ISO date filter. Defaults to last 30 days.")):
    return json.loads(get_draft_ipos(after=after))


@router.get("/ipos/active")
def ipos_active(after: str | None = Query(default=None, description="ISO date filter. Defaults to last 30 days.")):
    return json.loads(get_active_ipos(after=after))


@router.get("/ipos/{slug}")
def ipos_detail(slug: str):
    data = json.loads(get_ipo_detail_by_slug(slug))
    if data is None:
        raise HTTPException(status_code=404, detail="IPO not found")
    return data
