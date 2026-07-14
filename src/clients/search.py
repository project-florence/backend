import requests
from src.core.config import get_config
from pydantic import BaseModel
from typing import List


class NewsItem(BaseModel):
    title: str
    content: str
    url: str
    source_engine: str


def news_search(query: str, limit: int = 10) -> List[NewsItem]:
    url = get_config()["news_search"]["search_url"]
    params = {
        "q": query,
        "format": "json",
        "categories": "news",
        "safesearch": 1,
        "pageno": 1
    }

    headers = {
        "User-Agent": get_config()["news_search"]["user_agent"]
    }

    response = requests.get(url, params=params, headers=headers)

    if response.status_code == 200:
        data = response.json()
        results = data.get("results", [])[:limit]

        news_items = [NewsItem(**result) for result in results]
        return news_items
    else:
        print("Error:", response.status_code, response.text)
        return []


def news_to_str(news: List[NewsItem]) -> str:
    news_text = ""

    for i, item in enumerate(news, 1):
        news_text += f"\n--- News Item {i} ---\n"
        news_text += f"Title: {item.title}\n"
        news_text += f"Content: {item.content}\n"

    return news_text

def get_news_and_str(query: str, limit: int = 10):
    news_items = news_search(query, limit)
    news_text = news_to_str(news_items)

    return news_items, news_text