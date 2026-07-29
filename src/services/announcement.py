from pydantic import BaseModel
from datetime import datetime
from src.core.database import db


class Announcement(BaseModel):
    id: int
    title: str
    content: str
    sent_by: int | None
    created_at: datetime
    updated_at: datetime


def create_announcement(title: str, content: str, sent_by: int) -> Announcement | None:
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO announcements (title, content, sent_by) VALUES (%s, %s, %s) RETURNING id, title, content, sent_by, created_at, updated_at",
                (title, content, sent_by),
            )
            row = cur.fetchone()
            if row:
                return Announcement(id=row[0], title=row[1], content=row[2], sent_by=row[3], created_at=row[4], updated_at=row[5])
        return None
    except Exception:
        return None


def get_announcements() -> list[Announcement]:
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, title, content, sent_by, created_at, updated_at FROM announcements ORDER BY created_at DESC"
            )
            return [Announcement(id=r[0], title=r[1], content=r[2], sent_by=r[3], created_at=r[4], updated_at=r[5]) for r in cur.fetchall()]
    except Exception:
        return []


def get_announcement(announcement_id: int) -> Announcement | None:
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, title, content, sent_by, created_at, updated_at FROM announcements WHERE id = %s",
                (announcement_id,),
            )
            row = cur.fetchone()
            if row:
                return Announcement(id=row[0], title=row[1], content=row[2], sent_by=row[3], created_at=row[4], updated_at=row[5])
        return None
    except Exception:
        return None


def update_announcement(announcement_id: int, title: str, content: str) -> bool:
    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE announcements SET title = %s, content = %s, updated_at = NOW() WHERE id = %s",
                (title, content, announcement_id),
            )
            return cur.rowcount > 0
    except Exception:
        return False


def delete_announcement(announcement_id: int) -> bool:
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM announcements WHERE id = %s", (announcement_id,))
            return cur.rowcount > 0
    except Exception:
        return False
