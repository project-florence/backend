import threading
import time
from collections.abc import Generator

from fastapi import Depends, HTTPException

from src.api.deps import get_current_user
from src.core.redis import r


_fallback_lock = threading.Lock()
_fallback_slots: dict[str, float] = {}


def _acquire(kind: str, user_id: int, ttl_seconds: int) -> bool:
    key = f"job-slot:{kind}:{user_id}"
    try:
        conn = r._get_conn()
    except Exception:
        conn = None
    if conn is not None:
        return bool(conn.set(key, "1", nx=True, ex=ttl_seconds))

    now = time.monotonic()
    with _fallback_lock:
        expires_at = _fallback_slots.get(key, 0)
        if expires_at > now:
            return False
        _fallback_slots[key] = now + ttl_seconds
        if len(_fallback_slots) > 10_000:
            for stale_key, stale_expiry in list(_fallback_slots.items()):
                if stale_expiry <= now:
                    _fallback_slots.pop(stale_key, None)
    return True


def _release(kind: str, user_id: int) -> None:
    key = f"job-slot:{kind}:{user_id}"
    try:
        conn = r._get_conn()
    except Exception:
        conn = None
    if conn is not None:
        conn.delete(key)
        return
    with _fallback_lock:
        _fallback_slots.pop(key, None)


def require_job_slot(kind: str, ttl_seconds: int):
    def dependency(current_user_id: int = Depends(get_current_user)) -> Generator[None, None, None]:
        if not _acquire(kind, current_user_id, ttl_seconds):
            raise HTTPException(status_code=429, detail="A job of this type is already running")
        try:
            yield
        finally:
            _release(kind, current_user_id)

    return dependency
