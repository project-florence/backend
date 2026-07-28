from fastapi import APIRouter, Body, Request
from src.services.analytics import track_event

router = APIRouter()


@router.post("/analytics/event")
def analytics_event(request: Request, payload: list[dict] = Body(...)):
    user_id = getattr(request.state, "user_id", None)
    for event in payload:
        track_event(
            event_type=event.get("event_type", "unknown"),
            user_id=event.get("user_id") or user_id,
            ticker=event.get("ticker"),
            details=event.get("details"),
        )
    return {"received": len(payload)}
