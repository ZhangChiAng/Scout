"""Keyword filtering with ASCII-aware boundaries."""

import re

from .model import NewsItem


class KeywordFilter:
    def __init__(self, keywords: tuple[str, ...], fields: tuple[str, ...]) -> None:
        self.fields = fields
        self._patterns = tuple(
            re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(keyword)}(?![A-Za-z0-9_])",
                re.IGNORECASE,
            )
            for keyword in keywords
        )

    def matches(self, item: NewsItem) -> bool:
        searchable = "\n".join(getattr(item, field) for field in self.fields)
        return any(pattern.search(searchable) for pattern in self._patterns)

    def select(self, items: list[NewsItem]) -> list[NewsItem]:
        return [item for item in items if self.matches(item)]
