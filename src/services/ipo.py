import json
from datetime import datetime, timedelta, timezone

from src.core.redis import r
from src.core.config import get_config
from src.clients.ipo import list_ipos, get_ipo_detail


def _default_after() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")


def _cache_key_list(category: str, after: str) -> str:
    return f"halkarz:list:{category}:{after}"


def _cache_key_detail(slug: str) -> str:
    return f"halkarz:detail:{slug}"


def get_upcoming_ipos(after: str | None = None) -> str:
    if after is None:
        after = _default_after()

    key = _cache_key_list("ana-pazar", after)
    cached = r.get(key)
    if cached:
        return cached

    data = list_ipos("ana-pazar", after=after)
    serialized = json.dumps(data, ensure_ascii=False)
    cfg = get_config()["halkarz"]
    r.set(key, serialized, ex=cfg["list_cache_ttl"])
    return serialized


def get_draft_ipos(after: str | None = None) -> str:
    if after is None:
        after = _default_after()

    key = _cache_key_list("taslak", after)
    cached = r.get(key)
    if cached:
        return cached

    data = list_ipos("taslak", after=after)
    serialized = json.dumps(data, ensure_ascii=False)
    cfg = get_config()["halkarz"]
    r.set(key, serialized, ex=cfg["list_cache_ttl"])
    return serialized


def get_ipo_detail_by_slug(slug: str) -> str:
    key = _cache_key_detail(slug)
    cached = r.get(key)
    if cached:
        return cached

    data = get_ipo_detail(slug)
    if data is None:
        return "null"

    serialized = json.dumps(data, ensure_ascii=False)
    cfg = get_config()["halkarz"]
    r.set(key, serialized, ex=cfg["detail_cache_ttl"])
    return serialized
