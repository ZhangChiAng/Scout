"""Domain models shared across Scout components."""

import hashlib
import json
from dataclasses import asdict, dataclass
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
    """A normalized RSS issue with a stable delivery identity."""

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
class DigestArticle:
    """One overview entry enriched with matching Juya body content."""

    digest_key: str
    position: int
    number: str
    category: str
    title: str
    article_url: str
    summary: str
    detail: str
    related_links: tuple[tuple[str, str], ...]
    article_key: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class PersonalizedEvaluation:
    """A deliberately coarse, explainable recommendation for one article."""

    article_key: str
    verdict: str
    reason: str

    def __post_init__(self) -> None:
        if self.verdict not in {"推荐", "不推荐", "不确定"}:
            raise ValueError("evaluation verdict must be 推荐, 不推荐, or 不确定")
        reason = self.reason.strip()
        if not reason:
            raise ValueError("evaluation reason must not be empty")
        object.__setattr__(self, "reason", reason)


PROFILE_FORMAT_VERSION = 2
PROFILE_CATEGORIES = ("rules", "entities", "interests", "questions")
PROFILE_LABELS = ("判断规则", "具体对象", "专题兴趣", "重要疑问")


@dataclass(frozen=True, slots=True)
class PreferenceEntry:
    entry_id: str
    category: str
    text: str
    evidence_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PreferenceUpdate:
    mode: str
    trigger: str
    feedback_count: int
    revision_count: int
    revisions_since_rebuild: int
    last_full_feedback_revision_id: int
    input_chars: int


@dataclass(frozen=True, slots=True)
class PreferenceProfile:
    """Versioned, evidence-backed preferences for Scout's sole owner."""

    version: int
    like_rules: tuple[str, ...] = ()
    dislike_rules: tuple[str, ...] = ()
    tradeoffs: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    evidence_ids: tuple[int, ...] = ()
    change_summary: str = ""
    last_feedback_revision_id: int = 0
    notified: bool = False
    format_version: int = 1
    entries: tuple[PreferenceEntry, ...] = ()
    update: PreferenceUpdate | None = None

    @classmethod
    def empty(cls) -> PreferenceProfile:
        return cls(
            version=0,
            like_rules=(),
            dislike_rules=(),
            tradeoffs=(),
            uncertainties=("反馈尚不足，暂时依据条目本身谨慎判断。",),
            evidence_ids=(),
            change_summary="尚未形成偏好档案",
        )

    def as_prompt_dict(self) -> dict[str, object]:
        """Only readable preferences; never historical evidence or change notes."""

        if self.format_version == PROFILE_FORMAT_VERSION:
            return {
                "format_version": self.format_version,
                "version": self.version,
                **{
                    category: [e.text for e in self.entries if e.category == category]
                    for category in PROFILE_CATEGORIES
                },
            }
        return {
            "format_version": self.format_version,
            "version": self.version,
            "like_rules": list(self.like_rules),
            "dislike_rules": list(self.dislike_rules),
            "tradeoffs": list(self.tradeoffs),
            "uncertainties": list(self.uncertainties),
        }

    def as_entry_dict(self) -> dict[str, object]:
        """Compact identified entries for incremental input and persisted content."""

        if self.format_version != PROFILE_FORMAT_VERSION:
            return self.as_prompt_dict()
        return {
            "format_version": self.format_version,
            "version": self.version,
            **{
                category: [
                    {"id": e.entry_id, "text": e.text}
                    for e in self.entries
                    if e.category == category
                ]
                for category in PROFILE_CATEGORIES
            },
        }

    def as_display_dict(self) -> dict[str, object]:
        return {
            **self.as_entry_dict(),
            "change_summary": self.change_summary,
            "last_feedback_revision_id": self.last_feedback_revision_id,
            "notified": self.notified,
            "evidence_count": len(self.evidence_ids),
            "rule_count": self.rule_count,
            "entry_count": sum(len(values) for _, values in self.readable_sections()),
            "readable_chars": len(
                json.dumps(
                    self.as_prompt_dict(), ensure_ascii=False, separators=(",", ":")
                )
            ),
            "update": asdict(self.update) if self.update else None,
        }

    @property
    def rule_count(self) -> int:
        if self.format_version == PROFILE_FORMAT_VERSION:
            return sum(e.category == "rules" for e in self.entries)
        return len(self.like_rules) + len(self.dislike_rules) + len(self.tradeoffs)

    def readable_sections(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        if self.format_version == PROFILE_FORMAT_VERSION:
            return tuple(
                (label, tuple(e.text for e in self.entries if e.category == category))
                for category, label in zip(
                    PROFILE_CATEGORIES, PROFILE_LABELS, strict=True
                )
            )
        return (
            ("喜欢规则", self.like_rules),
            ("不喜欢规则", self.dislike_rules),
            ("权衡项", self.tradeoffs),
            ("不确定项", self.uncertainties),
        )


@dataclass(frozen=True, slots=True)
class CardDelivery:
    delivery_id: int
    snapshot_id: int
    article: DigestArticle
    purpose: str
    message_id: str
    chat_id: str
    verdict: str
    reason: str


@dataclass(frozen=True, slots=True)
class FeedbackEvidence:
    revision_id: int
    delivery_id: int
    article_key: str
    sentiment: str
    reason: str
    title: str
    category: str
    summary: str
    detail: str
    created_at: str


@dataclass(frozen=True, slots=True)
class FeedbackWrite:
    revision_id: int
    created: bool
    sentiment: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    """Preference inputs fixed by one completed read transaction."""

    active: PreferenceProfile | None = None
    feedback: tuple[FeedbackEvidence, ...] = ()
    cutoff_revision_id: int = 0
    next_version: int = 1
    new_revision_count: int = 0
    revisions_since_rebuild: int = 0
    edited_processed_feedback: bool = False
    rolled_back: bool = False

    @property
    def new_feedback(self) -> tuple[FeedbackEvidence, ...]:
        processed = self.active.last_feedback_revision_id if self.active else 0
        return tuple(f for f in self.feedback if f.revision_id > processed)


@dataclass(slots=True)
class RunStats:
    sent: int = 0
    previewed: int = 0
    failed: int = 0
    baseline: int = 0
    skipped: int = 0


def make_article_key(
    *, digest_key: str, position: int, number: str, title: str, article_url: str
) -> str:
    """Build the stable identity described by Scout's personalization spec."""

    normalized_url = canonicalize_url(article_url) if article_url.strip() else ""
    if normalized_url:
        return normalized_url
    if number.strip():
        return f"{digest_key}#number:{number.strip()}"
    title_hash = hashlib.sha256(title.strip().encode("utf-8")).hexdigest()[:16]
    return f"{digest_key}#position:{position}:{title_hash}"


def make_article_content_hash(
    *,
    category: str,
    title: str,
    article_url: str,
    summary: str,
    detail: str,
    related_links: tuple[tuple[str, str], ...],
) -> str:
    payload = json.dumps(
        {
            "category": category,
            "title": title,
            "article_url": canonicalize_url(article_url) if article_url else "",
            "summary": summary,
            "detail": detail,
            "related_links": related_links,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
