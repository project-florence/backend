import redis
from src.core.config import get_config


class _RedisProxy:
    _conn = None

    def _get_conn(self):
        if self._conn is None:
            cfg = get_config()["redis"]
            self._conn = redis.Redis(
                host=cfg["host"],
                port=cfg["port"],
                db=cfg["db"],
                decode_responses=cfg["decode_responses"],
            )
        return self._conn

    def __getattr__(self, name):
        return getattr(self._get_conn(), name)


r = _RedisProxy()
