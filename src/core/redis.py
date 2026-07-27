import os
import redis


class _RedisProxy:
    _conn = None

    def _get_conn(self):
        if self._conn is None:
            self._conn = redis.Redis(
                host=os.getenv("REDIS_HOST"),
                port=int(os.getenv("REDIS_PORT")),
                db=int(os.getenv("REDIS_DB")),
                decode_responses=os.getenv("REDIS_DECODE_RESPONSES", "true").lower() == "true",
            )
        return self._conn

    def __getattr__(self, name):
        return getattr(self._get_conn(), name)


r = _RedisProxy()
