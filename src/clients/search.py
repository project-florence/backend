import logging
import os

from pydantic import BaseModel
from typing import List

from src.clients.http import get_client
from src.core.config import get_config

logger = logging.getLogger(__name__)


class NewsItem(BaseModel):
    title: str
    content: str
    url: str
    source_engine: str = "web"


async def news_search(query: str, limit: int = 10) -> List[NewsItem]:
    url = os.getenv("NEWS_SEARCH_URL") or get_config()["news_search"]["search_url"]
    params = {
        "q": query,
        "format": "json",
        "categories": "news",
        "safesearch": 1,
        "pageno": 1
    }

    headers = {
        "User-Agent": os.getenv("NEWS_SEARCH_USER_AGENT") or get_config()["news_search"]["user_agent"],
        # SearXNG botdetection (behind-proxy mode) requires a client-IP header;
        # without X-Forwarded-For/X-Real-IP it returns 403 for every request.
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
    }

    client = await get_client()
    response = await client.get(url, params=params, headers=headers, timeout=10)

    if response.status_code == 200:
        data = response.json()
        results = data.get("results", [])[:limit]

        def _to_item(r: dict) -> dict:
            if "engine" in r and "source_engine" not in r:
                r["source_engine"] = r.pop("engine")
            return r

        news_items = [NewsItem(**_to_item(result)) for result in results]
        return news_items
    else:
        logger.error("SearXNG error: %s %s", response.status_code, response.text)
        return []


def news_to_str(news: List[NewsItem]) -> str:
    news_text = ""

    for i, item in enumerate(news, 1):
        news_text += f"\n--- News Item {i} ---\n"
        news_text += f"Title: {item.title}\n"
        news_text += f"Content: {item.content}\n"

    return news_text


async def get_news_and_str(query: str, limit: int = 10):
    news_items = await news_search(query, limit)
    news_text = news_to_str(news_items)

    return news_items, news_text
