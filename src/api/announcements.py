from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from src.core.database import db
from src.api.deps import get_current_user
from src.services.announcement import create_announcement, get_announcements, get_announcement, update_announcement, delete_announcement

router = APIRouter()


class AnnouncementCreate(BaseModel):
    title: str
    content: str


class AnnouncementUpdate(BaseModel):
    title: str
    content: str


def _is_admin(user_id: int) -> bool:
    with db.cursor() as cur:
        cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row is not None and row[0] == "admin"


@router.get("/announcements")
def list_announcements(current_user_id: int = Depends(get_current_user)):
    try:
        with db.cursor() as cur:
            cur.execute("SELECT created_at FROM users WHERE id = %s", (current_user_id,))
            user_row = cur.fetchone()
            user_created_at = user_row[0] if user_row else None

            cur.execute("SELECT last_announcement_viewed_at FROM users WHERE id = %s", (current_user_id,))
            viewed_row = cur.fetchone()
            last_viewed = viewed_row[0] if viewed_row else None

            cur.execute(
                "SELECT id, title, content, sent_by, created_at, updated_at FROM announcements ORDER BY created_at DESC"
            )
            rows = cur.fetchall()

        result = []
        for r in rows:
            ann_created = r[4]
            if user_created_at and ann_created < user_created_at:
                continue

            is_unread = last_viewed is None or ann_created > last_viewed

            result.append({
                "id": r[0],
                "title": r[1],
                "content": r[2],
                "sent_by": r[3],
                "created_at": r[4].isoformat(),
                "updated_at": r[5].isoformat(),
                "is_unread": is_unread,
            })

        return {"announcements": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/announcements/{announcement_id}")
def get_single_announcement(announcement_id: int, current_user_id: int = Depends(get_current_user)):
    ann = get_announcement(announcement_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="Announcement not found")
    return ann


@router.post("/announcements")
def create_new_announcement(body: AnnouncementCreate, current_user_id: int = Depends(get_current_user)):
    if not _is_admin(current_user_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    ann = create_announcement(body.title, body.content, current_user_id)
    if ann is None:
        raise HTTPException(status_code=500, detail="Failed to create announcement")
    return ann


@router.put("/announcements/{announcement_id}")
def update_existing_announcement(announcement_id: int, body: AnnouncementUpdate, current_user_id: int = Depends(get_current_user)):
    if not _is_admin(current_user_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    if not update_announcement(announcement_id, body.title, body.content):
        raise HTTPException(status_code=404, detail="Announcement not found")
    return {"message": "Announcement updated"}


@router.delete("/announcements/{announcement_id}")
def delete_existing_announcement(announcement_id: int, current_user_id: int = Depends(get_current_user)):
    if not _is_admin(current_user_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    if not delete_announcement(announcement_id):
        raise HTTPException(status_code=404, detail="Announcement not found")
    return {"message": "Announcement deleted"}


@router.post("/announcements/read")
def mark_announcements_read(current_user_id: int = Depends(get_current_user)):
    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_announcement_viewed_at = NOW() WHERE id = %s",
                (current_user_id,),
            )
        return {"message": "Marked as read"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
