from fastapi import APIRouter, Body, HTTPException, Request

from src.services.analytics import fire_and_forget

router = APIRouter()


@router.post("/analytics/event")
async def analytics_event(request: Request, payload: list[dict] = Body(...)):
    if len(payload) > 100:
        raise HTTPException(status_code=413, detail="Too many events")

    user_id = getattr(request.state, "user_id", None)
    for event in payload:
        fire_and_forget(
            event_type=event.get("event_type", "unknown"),
            user_id=user_id,
            ticker=event.get("ticker"),
            details=event.get("details"),
        )
    return {"received": len(payload)}
