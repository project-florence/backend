from fastapi import APIRouter

from src.services.market import get_market_status_payload

router = APIRouter()


@router.get("/market/status")
async def market_status():
    """BIST acik/kapali durumu + sonraki acilis ani (public)."""
    return await get_market_status_payload()
