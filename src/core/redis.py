"""Async Redis erisimi (redis.asyncio).

Eski sync proxy'nin yerine gecer. Redis yoksa veya baglanti kurulamazsa tum
cagrilar ``None`` doner (cache'siz calisma modu korunur). Kullanim:
``await r.get(key)``, ``await r.set(key, value, ex=...)`` vb.
"""

import asyncio
import logging
import os
import time

import redis.asyncio as aioredis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Baglanti denemeleri arasi bekleme (saniye): basarisiz ping/kullanim sonrasi
# Redis kalici devre disi kalmaz; cooldown gecince bir sonraki cagri yeniden
# baglanmayi dener.
_RETRY_COOLDOWN = 60


class _AsyncRedisProxy:
    def __init__(self) -> None:
        self._conn: aioredis.Redis | None = None
        self._disabled = False
        self._retry_after = 0.0
        self._lock = asyncio.Lock()

    async def _get_conn(self) -> aioredis.Redis | None:
        if self._disabled:
            if time.monotonic() < self._retry_after:
                return None
            # Cooldown gecti: yeniden baglanmayi dene.
            self._disabled = False
        if self._conn is None:
            async with self._lock:
                if self._conn is None:
                    try:
                        conn = aioredis.Redis(
                            host=os.getenv("REDIS_HOST"),
                            port=int(os.getenv("REDIS_PORT")),
                            db=int(os.getenv("REDIS_DB")),
                            password=os.getenv("REDIS_PASSWORD") or None,
                            decode_responses=os.getenv("REDIS_DECODE_RESPONSES", "true").lower() == "true",
                            socket_connect_timeout=2,
                            socket_timeout=2,
                        )
                        await conn.ping()
                        self._conn = conn
                        self._disabled = False
                        logger.info("Redis connection established")
                    except Exception as e:  # redis.RedisError / OSError
                        logger.error("Redis connection failed: %s. Running without cache.", e)
                        self._disabled = True
                        self._retry_after = time.monotonic() + _RETRY_COOLDOWN
                        self._conn = None
                        return None
        return self._conn

    async def _call(self, method: str, *args, **kwargs):
        conn = await self._get_conn()
        if conn is None:
            return None
        try:
            return await getattr(conn, method)(*args, **kwargs)
        except RedisError as e:
            # Baglanti/tasiyici hatasi: conn'u dusur ki bir sonraki cagri
            # yeniden baglansin (cooldown sonrasi). Cache'siz mod (None) korunur.
            self._conn = None
            self._disabled = True
            self._retry_after = time.monotonic() + _RETRY_COOLDOWN
            logger.warning("Redis %s failed: %s. Will retry in %ss.", method, e, _RETRY_COOLDOWN)
            return None
        except Exception as e:
            logger.debug("Redis %s failed: %s", method, e)
            return None

    # --- Kullanilan komutlar (ihtiyac halinde eklenebilir) -----------------
    async def get(self, key):
        return await self._call("get", key)

    async def set(self, key, value, ex=None, nx=False, xx=False):
        return await self._call("set", key, value, ex=ex, nx=nx, xx=xx)

    async def delete(self, *keys):
        return await self._call("delete", *keys)

    async def incr(self, key):
        return await self._call("incr", key)

    async def expire(self, key, seconds):
        return await self._call("expire", key, seconds)

    async def sadd(self, key, *values):
        return await self._call("sadd", key, *values)

    async def srem(self, key, *values):
        return await self._call("srem", key, *values)

    async def smembers(self, key):
        return await self._call("smembers", key)

    async def sismember(self, key, value):
        return await self._call("sismember", key, value)


r = _AsyncRedisProxy()
