import requests
from src.config import get_config


def search_gdelt_articles(
    query: str,
    max_records: int | None = None,
) -> dict:
    cfg = get_config()["gdelt_api"]
    if max_records is None:
        max_records = cfg["max_records"]

    params = {
        "query": query,
        "mode": cfg["mode"],
        "format": cfg["format"],
        "maxrecords": max_records,
    }
    resp = requests.get(cfg["base_url"], params=params, timeout=cfg["timeout"])
    resp.raise_for_status()
    return resp.json()
