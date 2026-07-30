import os
import redis
import logging

logger = logging.getLogger(__name__)


class _RedisFallback:
    def __init__(self, method_name: str):
        self._method = method_name

    def __call__(self, *args, **kwargs):
        logger.debug("Redis %s called but unavailable, returning None", self._method)
        return None


class _RedisProxy:
    _conn = None
    _disabled = False

    def _get_conn(self):
        if self._disabled:
            return None
        if self._conn is None:
            try:
                self._conn = redis.Redis(
                    host=os.getenv("REDIS_HOST"),
                    port=int(os.getenv("REDIS_PORT")),
                    db=int(os.getenv("REDIS_DB")),
                    password=os.getenv("REDIS_PASSWORD") or None,
                    decode_responses=os.getenv("REDIS_DECODE_RESPONSES", "true").lower() == "true",
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                self._conn.ping()
                logger.info("Redis connection established")
            except redis.RedisError as e:
                logger.error("Redis connection failed: %s. Running without cache.", e)
                self._disabled = True
                self._conn = None
                return None
        return self._conn

    def _is_available(self) -> bool:
        return not self._disabled

    def __getattr__(self, name):
        conn = self._get_conn()
        if conn is None:
            return _RedisFallback(name)
        return getattr(conn, name)


r = _RedisProxy()
