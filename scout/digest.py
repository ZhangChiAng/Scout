"""Parse curated AI daily issue HTML into a structured digest.

The Juya AI daily feed ships one item per day whose ``content:encoded`` holds
the full issue page: a cover image, an ``<h1>``, a ``概览`` section grouping
headlines by category, and article sections with a blockquote summary,
paragraph detail, images, and related links.  This module extracts the
structure with the standard-library ``HTMLParser`` so the app can render a
Feishu message from the overview today and personalize per-article later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

_ISSUE_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")
_OVERVIEW_HEADING = "概览"
_FULL_PAGE_LINK_TEXT = "查看网页全文"


@dataclass(frozen=True, slots=True)
class OverviewEntry:
    """One overview headline: category, linked headline, and code number."""

    category: str
    headline: str
    url: str
    number: str


@dataclass(frozen=True, slots=True)
class RelatedLink:
    text: str
    url: str


@dataclass(frozen=True, slots=True)
class Section:
    """One article section from the issue body."""

    title: str
    url: str
    summary: str
    detail: str
    related_links: tuple[RelatedLink, ...]


@dataclass(frozen=True, slots=True)
class IssueDigest:
    """The parsed structure of one daily issue."""

    issue_date: str
    overview: tuple[OverviewEntry, ...]
    sections: tuple[Section, ...]
    page_url: str


class DigestError(ValueError):
    """Raised when issue HTML cannot be parsed into a usable digest."""


def parse_issue(html: str, *, page_url: str = "") -> IssueDigest:
    """Parse one issue's ``content:encoded`` HTML into a structured digest.

    Bad or truncated HTML never raises; the parser simply yields fewer
    entries.  ``page_url`` overrides any trailing full-page link.
    """

    parser = _DigestParser()
    parser.feed(html)
    parser.close()
    resolved_page_url = page_url.strip() or parser.full_page_url
    return IssueDigest(
        issue_date=parser.issue_date,
        overview=tuple(parser.overview),
        sections=tuple(parser.sections),
        page_url=resolved_page_url,
    )


def _clean_text(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


class _DigestParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.issue_date = ""
        self.overview: list[OverviewEntry] = []
        self.sections: list[Section] = []
        self.full_page_url = ""

        self._h1: list[str] | None = None
        self._h2: list[str] | None = None
        self._h3: list[str] | None = None
        self._h3_url = ""
        self._li: list[str] | None = None
        self._li_url = ""
        self._li_number = ""
        self._li_link_text = ""
        self._blockquote: list[str] | None = None
        self._p: list[str] | None = None
        self._link_stack: list[tuple[str, list[str], str]] = []
        self._code_depth = 0
        self._overview_mode = False
        self._category = ""
        self._section: dict[str, object] | None = None

    # -- tag handlers -----------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "h1":
            self._h1 = []
        elif tag == "h2":
            self._h2 = []
        elif tag == "h3":
            self._h3 = []
            self._h3_url = ""
        elif tag == "li":
            self._finish_li()
            self._li = []
            self._li_url = ""
            self._li_number = ""
            self._li_link_text = ""
        elif tag == "blockquote" and self._section is not None:
            self._blockquote = []
        elif tag == "p" and self._section is not None and not self._overview_mode:
            self._p = []
        elif tag == "a":
            href = attributes.get("href", "").strip()
            self._link_stack.append((href, [], ""))
            if self._h3 is not None and not self._h3_url and href:
                self._h3_url = href
            if self._li is not None and not self._li_url and href:
                self._li_url = href
        elif tag == "code":
            self._code_depth += 1

    def handle_data(self, data: str) -> None:
        if self._code_depth > 0:
            if self._li is not None:
                self._li_number += data
            return
        if self._link_stack:
            self._link_stack[-1][1].append(data)
        if self._li is not None and self._link_stack:
            data = ""
        if self._p is not None and self._link_stack:
            data = ""
        for target in (
            self._h1,
            self._h2,
            self._h3,
            self._li,
            self._blockquote,
            self._p,
        ):
            if target is not None:
                target.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link_stack:
            href, text_parts, _ = self._link_stack.pop()
            text = _clean_text(text_parts)
            if self._li is not None and text and text != "↗":
                self._li_link_text = text
            if text == _FULL_PAGE_LINK_TEXT and href and not self.full_page_url:
                self.full_page_url = href
        elif tag == "h1":
            text = _clean_text(self._h1 or [])
            self._h1 = None
            match = _ISSUE_DATE_PATTERN.search(text)
            if match:
                self.issue_date = match.group(1)
        elif tag == "h2":
            text = _clean_text(self._h2 or [])
            self._h2 = None
            if text == _OVERVIEW_HEADING:
                self._overview_mode = True
                self._category = ""
                self._finish_section()
            else:
                self._overview_mode = False
                self._finish_section()
        elif tag == "h3":
            text = _clean_text(self._h3 or [])
            self._h3 = None
            if not text:
                return
            if self._overview_mode:
                self._category = text
            else:
                self._finish_section()
                self._section = {
                    "title": text,
                    "url": self._h3_url,
                    "summary": "",
                    "detail": "",
                    "related_links": [],
                }
        elif tag == "li":
            self._finish_li()
        elif tag == "code" and self._code_depth > 0:
            self._code_depth -= 1
        elif tag == "blockquote":
            if self._blockquote is not None and self._section is not None:
                text = _clean_text(self._blockquote)
                if text and not self._section["summary"]:
                    self._section["summary"] = text
            self._blockquote = None
        elif tag == "p":
            if self._p is not None and self._section is not None:
                text = _clean_text(self._p)
                if text and not self._section["detail"]:
                    self._section["detail"] = text
            self._p = None

    def _finish_li(self) -> None:
        if self._li is None:
            return
        text = _clean_text(self._li)
        self._li = None
        if not text and getattr(self, "_li_link_text", ""):
            text = self._li_link_text
        if not text:
            return
        if self._overview_mode:
            self.overview.append(
                OverviewEntry(
                    category=self._category,
                    headline=text,
                    url=self._li_url,
                    number=self._li_number.strip().lstrip("#").strip(),
                )
            )
        elif self._section is not None:
            self._section["related_links"].append(  # type: ignore[attr-defined]
                RelatedLink(text=text, url=self._li_url)
            )
        self._li_url = ""
        self._li_number = ""

    def _finish_section(self) -> None:
        section = self._section
        self._section = None
        if section is None:
            return
        self.sections.append(
            Section(
                title=str(section["title"]),
                url=str(section["url"]),
                summary=str(section["summary"]),
                detail=str(section["detail"]),
                related_links=tuple(section["related_links"]),  # type: ignore[arg-type]
            )
        )

    def close(self) -> None:
        self._finish_li()
        self._finish_section()
        super().close()
