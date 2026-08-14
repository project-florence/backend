from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.deps import get_current_user
from src.core.database import db
from src.services.announcement import create_announcement, get_announcements, get_announcement, update_announcement, delete_announcement

router = APIRouter()


class AnnouncementCreate(BaseModel):
    title: str
    content: str


class AnnouncementUpdate(BaseModel):
    title: str
    content: str


async def _is_admin(user_id: int) -> bool:
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
        row = await cur.fetchone()
        return row is not None and row[0] == "admin"


@router.get("/announcements")
async def list_announcements(current_user_id: int = Depends(get_current_user)):
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute("SELECT created_at FROM users WHERE id = %s", (current_user_id,))
            user_row = await cur.fetchone()
            user_created_at = user_row[0] if user_row else None

            await cur.execute("SELECT last_announcement_viewed_at FROM users WHERE id = %s", (current_user_id,))
            viewed_row = await cur.fetchone()
            last_viewed = viewed_row[0] if viewed_row else None

            await cur.execute(
                "SELECT id, title, content, sent_by, created_at, updated_at FROM announcements ORDER BY created_at DESC"
            )
            rows = await cur.fetchall()

        result = []
        for r in rows:
            ann_created = r[4]
            cutoff = user_created_at - timedelta(days=7) if user_created_at else None
            if cutoff and ann_created < cutoff:
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
    except Exception:
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/announcements/{announcement_id}")
async def get_single_announcement(announcement_id: int, current_user_id: int = Depends(get_current_user)):
    ann = await get_announcement(announcement_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="Announcement not found")
    return ann


@router.post("/announcements")
async def create_new_announcement(body: AnnouncementCreate, current_user_id: int = Depends(get_current_user)):
    if not await _is_admin(current_user_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    ann = await create_announcement(body.title, body.content, current_user_id)
    if ann is None:
        raise HTTPException(status_code=500, detail="Failed to create announcement")
    return ann


@router.put("/announcements/{announcement_id}")
async def update_existing_announcement(announcement_id: int, body: AnnouncementUpdate, current_user_id: int = Depends(get_current_user)):
    if not await _is_admin(current_user_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    if not await update_announcement(announcement_id, body.title, body.content):
        raise HTTPException(status_code=404, detail="Announcement not found")
    return {"message": "Announcement updated"}


@router.delete("/announcements/{announcement_id}")
async def delete_existing_announcement(announcement_id: int, current_user_id: int = Depends(get_current_user)):
    if not await _is_admin(current_user_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    if not await delete_announcement(announcement_id):
        raise HTTPException(status_code=404, detail="Announcement not found")
    return {"message": "Announcement deleted"}


@router.post("/announcements/read")
async def mark_announcements_read(current_user_id: int = Depends(get_current_user)):
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "UPDATE users SET last_announcement_viewed_at = NOW() WHERE id = %s",
                (current_user_id,),
            )
            # Commit blok icinde (otomatik iade rollback etmesin).
            await db.commit()
        return {"message": "Marked as read"}
    except Exception:
        raise HTTPException(status_code=500, detail="Database error")
