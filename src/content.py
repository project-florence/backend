from datetime import datetime

class Content:
     title : str
     date : datetime
     content : str
     def __init__(self, title : str, date : datetime, content : str) -> None:
        self.title = title
        self.date = date
        self.content = content

     def to_string(self) -> str:
         return f"{self.title}\n {self.date.strftime('%Y-%m-%d')}\n {self.content}"