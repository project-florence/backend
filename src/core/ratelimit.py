import time
import threading
from collections import defaultdict
from fastapi import HTTPException


class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, key: str, max_requests: int, window_seconds: int):
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._buckets[key]
            bucket[:] = [t for t in bucket if t > cutoff]
            if len(bucket) >= max_requests:
                raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
            bucket.append(now)


rate_limiter = RateLimiter()
