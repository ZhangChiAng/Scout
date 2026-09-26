"""Collect and normalize the configured Juya RSS feed."""

from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from .config import NetworkConfig, SourceConfig
from .datetime_utils import beijing_isoformat
from .model import NewsItem, canonicalize_url

LOGGER = logging.getLogger(__name__)
TRANSIENT_FETCH_RETRY_DELAY_SECONDS = 3.0
FETCH_ATTEMPTS = 2


class CollectionError(RuntimeError):
    """Raised when an entire source cannot be downloaded or parsed."""


@dataclass(frozen=True, slots=True)
class CollectionIssue:
    """A malformed entry that does not invalidate the rest of its source."""

    source: str
    stage: str
    title: str = ""
    url: str = ""
    message: str = ""
    index: int | None = None


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    """Valid items plus independent entry-level parsing issues."""

    items: tuple[NewsItem, ...]
    issues: tuple[CollectionIssue, ...] = ()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag.lower() in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag.lower() in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def clean_html(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def normalize_date(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid publication date: {value!r}") from exc
    if parsed is None:
        raise ValueError(f"invalid publication date: {value!r}")
    return beijing_isoformat(parsed)


def _is_transient_fetch_error(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (URLError, OSError))


def _fetch_error_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


class RSSCollector:
    """Collect the first configured number of items in RSS feed order."""

    accept = "application/rss+xml, application/xml;q=0.9"
    response_name = "RSS"

    def __init__(
        self,
        source: SourceConfig,
        network: NetworkConfig,
    ) -> None:
        self.source = source
        self.network = network

    def _fetch_bytes(self) -> bytes:
        headers = {
            "User-Agent": self.network.user_agent,
            "Accept": self.accept,
        }
        payload = b""
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            request = Request(self.source.url, headers=headers, method="GET")
            try:
                with urlopen(request, timeout=self.network.timeout_seconds) as response:
                    payload = response.read(self.network.max_bytes + 1)
            except Exception as exc:
                if attempt < FETCH_ATTEMPTS and _is_transient_fetch_error(exc):
                    LOGGER.warning(
                        "transient %s fetch failure (attempt %d of %d): %s; retrying",
                        self.response_name,
                        attempt,
                        FETCH_ATTEMPTS,
                        _fetch_error_detail(exc),
                    )
                    time.sleep(TRANSIENT_FETCH_RETRY_DELAY_SECONDS)
                    continue
                raise CollectionError(
                    f"failed to fetch {self.response_name}: {_fetch_error_detail(exc)}"
                ) from exc
            break
        if len(payload) > self.network.max_bytes:
            raise CollectionError(
                f"{self.response_name} response exceeds "
                f"{self.network.max_bytes} byte limit"
            )
        return payload

    def _issue(
        self,
        index: int,
        error: Exception | str,
        *,
        title: str = "",
        url: str = "",
        stage: str = "parse",
    ) -> CollectionIssue:
        message = str(error)
        LOGGER.warning(
            "skipping invalid %s item %d: %s", self.response_name, index, message
        )
        return CollectionIssue(
            source=self.source.name,
            stage=stage,
            title=title,
            url=url,
            message=message,
            index=index,
        )

    def collect(self) -> CollectionBatch:
        payload = self._fetch_bytes()
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise CollectionError(f"invalid RSS XML: {exc}") from exc

        entries = [
            element for element in root.iter() if _local_name(element.tag) == "item"
        ]
        if not entries:
            raise CollectionError("invalid RSS XML: feed contains no item elements")
        items: list[NewsItem] = []
        issues: list[CollectionIssue] = []
        dedupe_keys: set[str] = set()
        for index, element in enumerate(entries[: self.source.window_size], start=1):
            try:
                item = self._map_item(element)
            except ValueError as exc:
                issues.append(
                    self._issue(
                        index,
                        exc,
                        title=clean_html(_optional_text(element, "title")),
                        url=_optional_text(element, "link").strip(),
                    )
                )
                continue
            if item.dedupe_key in dedupe_keys:
                continue
            dedupe_keys.add(item.dedupe_key)
            items.append(item)
        return CollectionBatch(tuple(items), tuple(issues))

    def _map_item(self, element: ET.Element) -> NewsItem:
        title = clean_html(_required_text(element, "title"))
        url = _allowed_item_url(
            _required_text(element, "link").strip(), self.source.url
        )
        published = normalize_date(_required_text(element, "pubDate").strip())
        if not title:
            raise ValueError("empty title")

        guid = _optional_text(element, "guid").strip()
        item_id = guid or canonicalize_url(url)
        if not item_id:
            raise ValueError("missing stable ID")

        encoded = _optional_text(element, "encoded")
        description = _optional_text(element, "description")
        content = encoded if encoded else description
        author = _optional_text(element, "creator") or _optional_text(element, "author")
        categories = [
            clean_html(child.text or "")
            for child in element
            if _local_name(child.tag) == "category" and clean_html(child.text or "")
        ]
        return NewsItem(
            source=self.source.name,
            item_id=item_id,
            title=title,
            content=content,
            url=url,
            published_at=published,
            author=clean_html(author),
            category=", ".join(categories),
            guid=guid,
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _optional_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return "".join(child.itertext())
    return ""


def _required_text(element: ET.Element, name: str) -> str:
    value = _optional_text(element, name)
    if not value.strip():
        raise ValueError(f"missing {name}")
    return value


def _allowed_item_url(target: str, base_url: str) -> str:
    value = html.unescape(target.strip()).strip("<>")
    if not value or value.startswith("#"):
        raise ValueError("link is not an article HTTP(S) URL")
    try:
        url = urljoin(base_url, value)
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("link is not a valid HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("link is not a valid HTTP(S) URL")
    return url


def collect_source(
    source: SourceConfig,
    network: NetworkConfig,
) -> CollectionBatch:
    """Collect the configured RSS source."""

    return RSSCollector(source, network).collect()
