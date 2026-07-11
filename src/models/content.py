from dataclasses import dataclass
from datetime import datetime


@dataclass
class Content:
    title: str
    date: datetime | None
    text: str

    def to_string(self):
        return f"{self.title}\n{self.date}\n{self.text}"
