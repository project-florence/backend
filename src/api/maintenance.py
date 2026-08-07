from fastapi import APIRouter

from src.services.maintenance import list_disabled

router = APIRouter()


@router.get("/maintenance")
async def get_maintenance():
    return {"disabled_features": await list_disabled()}
