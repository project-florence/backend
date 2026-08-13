import asyncio
import time
from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException

from src.api.deps import get_current_user
from src.core.redis import r


_fallback_lock = asyncio.Lock()
_fallback_slots: dict[str, float] = {}

# Redis lease'i kisa TTL ile alinir (120s) ve heartbeat task'i her 30s'de
# uzatir. Boylece uzun suren islerde (rapor 900s'e kadar) lease dusmez, ama
# is/cikis ani olurse lease 120s icinde serbest kalir (eski: 900s).
HEARTBEAT_TTL = 120
HEARTBEAT_INTERVAL = 30


async def _heartbeat_loop(kind: str, user_id: int) -> None:
    key = f"job-slot:{kind}:{user_id}"
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await r.expire(key, HEARTBEAT_TTL)
    except asyncio.CancelledError:
        raise


async def _acquire(kind: str, user_id: int, ttl_seconds: int) -> bool:
    key = f"job-slot:{kind}:{user_id}"
    lease_ttl = min(ttl_seconds, HEARTBEAT_TTL)
    acquired = await r.set(key, "1", nx=True, ex=lease_ttl)
    if acquired is not None:
        return bool(acquired)

    # Redis down ise proses ici fallback (heartbeat yok, tam sure kullanilir).
    now = time.monotonic()
    async with _fallback_lock:
        expires_at = _fallback_slots.get(key, 0)
        if expires_at > now:
            return False
        _fallback_slots[key] = now + ttl_seconds
        if len(_fallback_slots) > 10_000:
            for stale_key, stale_expiry in list(_fallback_slots.items()):
                if stale_expiry <= now:
                    _fallback_slots.pop(stale_key, None)
    return True


async def _release(kind: str, user_id: int, heartbeat: asyncio.Task | None = None) -> None:
    if heartbeat is not None and not heartbeat.done():
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
    key = f"job-slot:{kind}:{user_id}"
    deleted = await r.delete(key)
    if deleted is not None:
        return
    async with _fallback_lock:
        _fallback_slots.pop(key, None)


def require_job_slot(kind: str, ttl_seconds: int):
    async def dependency(current_user_id: int = Depends(get_current_user)) -> AsyncGenerator[None, None]:
        if not await _acquire(kind, current_user_id, ttl_seconds):
            raise HTTPException(status_code=429, detail="A job of this type is already running")
        heartbeat = asyncio.create_task(_heartbeat_loop(kind, current_user_id))
        try:
            yield
        finally:
            await _release(kind, current_user_id, heartbeat)

    return dependency
