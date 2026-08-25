from contextlib import AbstractContextManager
from io import BytesIO

from scout.model import NewsItem


class FakeResponse(AbstractContextManager["FakeResponse"]):
    def __init__(self, body: bytes) -> None:
        self._body = BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __exit__(self, *args: object) -> None:
        return None


def news_item(**overrides: str) -> NewsItem:
    values = {
        "source": "OpenAI News",
        "item_id": "guid-1",
        "title": "A GPT release",
        "content": "A useful API update.",
        "url": "https://example.com/news/1",
        "published_at": "2026-08-10T12:00:00+08:00",
        "author": "OpenAI",
        "category": "Product",
        "guid": "guid-1",
    }
    values.update(overrides)
    return NewsItem(**values)
