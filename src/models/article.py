from dataclasses import dataclass
from datetime import datetime


@dataclass
class Article:
    url: str
    title: str
    lang: str | None
    date: datetime | None
