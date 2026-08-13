from datetime import datetime

from pydantic import BaseModel

from src.core.database import db


class Announcement(BaseModel):
    id: int
    title: str
    content: str
    sent_by: int | None
    created_at: datetime
    updated_at: datetime


async def create_announcement(title: str, content: str, sent_by: int | None = None) -> Announcement | None:
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "INSERT INTO announcements (title, content, sent_by) VALUES (%s, %s, %s) RETURNING id, title, content, sent_by, created_at, updated_at",
                (title, content, sent_by),
            )
            row = await cur.fetchone()
        if row:
            await db.commit()
            return Announcement(id=row[0], title=row[1], content=row[2], sent_by=row[3], created_at=row[4], updated_at=row[5])
        return None
    except Exception:
        return None


async def get_announcements() -> list[Announcement]:
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT id, title, content, sent_by, created_at, updated_at FROM announcements ORDER BY created_at DESC"
            )
            return [Announcement(id=r[0], title=r[1], content=r[2], sent_by=r[3], created_at=r[4], updated_at=r[5]) for r in await cur.fetchall()]
    except Exception:
        return []


async def get_announcement(announcement_id: int) -> Announcement | None:
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT id, title, content, sent_by, created_at, updated_at FROM announcements WHERE id = %s",
                (announcement_id,),
            )
            row = await cur.fetchone()
            if row:
                return Announcement(id=row[0], title=row[1], content=row[2], sent_by=row[3], created_at=row[4], updated_at=row[5])
        return None
    except Exception:
        return None


async def update_announcement(announcement_id: int, title: str, content: str) -> bool:
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "UPDATE announcements SET title = %s, content = %s, updated_at = NOW() WHERE id = %s",
                (title, content, announcement_id),
            )
            rowcount = cur.rowcount
        if rowcount:
            await db.commit()
        return rowcount > 0
    except Exception:
        return False


async def delete_announcement(announcement_id: int) -> bool:
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute("DELETE FROM announcements WHERE id = %s", (announcement_id,))
            rowcount = cur.rowcount
        if rowcount:
            await db.commit()
        return rowcount > 0
    except Exception:
        return False
