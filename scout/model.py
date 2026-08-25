"""Domain models shared across Scout components."""

from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_QUERY_NAMES = {
    "_ga",
    "_gl",
    "_hsenc",
    "_hsmi",
    "dclid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "msclkid",
    "vero_conv",
    "vero_id",
}


def canonicalize_url(url: str) -> str:
    """Return the exact cross-source deduplication form of an article URL.

    Fragments and recognized analytics parameters do not identify different
    articles.  Other query parameters are retained and sorted, since they can
    legitimately select different official content.
    """

    value = url.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return value.split("#", 1)[0]
    if not parsed.scheme or not parsed.hostname:
        return value.split("#", 1)[0]

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not (
        scheme == "http" and port == 80 or scheme == "https" and port == 443
    ):
        host = f"{host}:{port}"
    query = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not name.lower().startswith("utm_")
        and name.lower() not in _TRACKING_QUERY_NAMES
    ]
    query.sort()
    path = parsed.path or "/"
    return urlunsplit((scheme, host, path, urlencode(query, doseq=True), ""))


@dataclass(frozen=True, slots=True)
class NewsItem:
    """A normalized feed entry.

    Every field is a string so collectors can be swapped without changing the
    filtering, storage, or notification boundaries.
    """

    source: str
    item_id: str
    title: str
    content: str
    url: str
    published_at: str
    author: str
    category: str
    guid: str
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        key = self.dedupe_key.strip() or canonicalize_url(self.url)
        if not key:
            raise ValueError("NewsItem.dedupe_key must not be empty")
        object.__setattr__(self, "dedupe_key", key)


@dataclass(frozen=True, slots=True)
class ChineseSummary:
    title_zh: str
    bullets_zh: tuple[str, ...]

    def apply_to(self, item: NewsItem) -> NewsItem:
        content = "\n".join(f"• {bullet}" for bullet in self.bullets_zh)
        return replace(item, title=self.title_zh, content=content)
