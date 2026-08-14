import asyncio
import time
from collections import defaultdict

from fastapi import HTTPException

from src.core.redis import r


class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check(self, key: str, max_requests: int, window_seconds: int, is_admin: bool = False, admin_multiplier: int = 10):
        now = time.time()
        cutoff = now - window_seconds

        # Admin kullanicilar cok daha yuksek limit alir (varsayilan 10x).
        limit = max_requests * (admin_multiplier if is_admin else 1)

        redis_key = f"ratelimit:{key}"
        try:
            count = await r.incr(redis_key)
            if count is not None:
                if count == 1:
                    await r.expire(redis_key, window_seconds)
                if count > limit:
                    raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
                return
        except HTTPException:
            raise
        except Exception:
            pass

        async with self._lock:
            bucket = self._buckets[key]
            bucket[:] = [t for t in bucket if t > cutoff]
            if len(bucket) >= limit:
                raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
            bucket.append(now)
            if len(self._buckets) > 10_000:
                stale_keys = [k for k, values in self._buckets.items() if not values or values[-1] <= cutoff]
                for stale_key in stale_keys:
                    self._buckets.pop(stale_key, None)


rate_limiter = RateLimiter()
