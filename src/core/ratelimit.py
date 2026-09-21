import asyncio
import os
import time
from collections import defaultdict

from fastapi import HTTPException, Request

from src.core.redis import r


def client_ip(request: Request) -> str:
    """Anonim IP limiti icin istemci IP'sini cozer (B-17).

    Uretimde uygulama nginx arkasinda; nginx ``X-Forwarded-For`` basligini
    ekler. Guven siniri ortam degiskeniyle yonetilir:

    - ``TRUST_PROXY_HEADERS`` (varsayilan acik): baslik varsa ILK deger
      (orijinal istemci) kullanilir. Bu, nginx'in basligi ``$remote_addr``
      ile KENDI yazdigi (istemcinin gonderdigi degeri ezdigi) kurulum icin
      dogrudur. ``$proxy_add_x_forwarded_for`` kullaniliyorsa istemci basligin
      basina sahte deger ekleyebilir; o durumda son deger alinmali ya da
      nginx ``real_ip`` modulu ile guvenilir proxy listesi tanimlanmalidir.
    - ``TRUST_PROXY_HEADERS=0``: basliga hic guvenilmez, dogrudan
      ``request.client.host`` kullanilir. Bu en guvenli secenektir ama tum
      anonim trafik nginx IP'sinde toplanip limit yanlis pozitif uretebilir.

    Baslik ve peer adresi yoksa ``"unknown"`` doner (tum boyle istekler ayni
    kovayi paylasir).
    """
    trust = os.getenv("TRUST_PROXY_HEADERS", "1").lower() not in ("0", "false", "no")
    if trust:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


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
                    # B-17/B-14: 429 yanitinda istemcinin geri cekilme (backoff)
                    # stratejisi icin saniye cinsinden Retry-After dondur.
                    raise HTTPException(
                        status_code=429,
                        detail="Too many requests. Please slow down.",
                        headers={"Retry-After": str(window_seconds)},
                    )
                return
        except HTTPException:
            raise
        except Exception:
            pass

        async with self._lock:
            bucket = self._buckets[key]
            bucket[:] = [t for t in bucket if t > cutoff]
            if len(bucket) >= limit:
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests. Please slow down.",
                    headers={"Retry-After": str(window_seconds)},
                )
            bucket.append(now)
            if len(self._buckets) > 10_000:
                stale_keys = [k for k, values in self._buckets.items() if not values or values[-1] <= cutoff]
                for stale_key in stale_keys:
                    self._buckets.pop(stale_key, None)


rate_limiter = RateLimiter()
