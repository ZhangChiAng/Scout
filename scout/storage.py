"""SQLite-backed delivery, article, feedback, and preference state."""

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from .model import (
    DigestArticle,
    NewsItem,
    PersonalizedEvaluation,
    PreferenceProfile,
    canonicalize_url,
)


class StorageError(RuntimeError):
    """Raised when the SQLite database has an unsupported legacy schema."""


SOURCE_FAILURE_ITEM_KEY = "__source__"
STATE_FAILURE_ITEM_KEY = "__state__"
BASELINE_FAILURE_ITEM_KEY = "__baseline__"
RECONCILE_FAILURE_ITEM_KEY = "__reconcile__"
UNSEEN_FAILURE_ITEM_KEY = "__unseen__"
AGE_FAILURE_ITEM_KEY = "__age__"

DELIVERED_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivered_items (
    delivery_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    url TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""

SOURCE_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT NOT NULL PRIMARY KEY,
    initialized_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""

BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS baseline_items (
    source TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    item_id TEXT NOT NULL,
    url TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (source, dedupe_key)
)
"""

ACTIVE_FAILURES_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_failures (
    source TEXT NOT NULL,
    item_key TEXT NOT NULL,
    alerted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (source, item_key)
)
"""

ARTICLE_SNAPSHOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS article_snapshots (
    snapshot_id INTEGER PRIMARY KEY,
    digest_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    number TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    article_url TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail TEXT NOT NULL,
    related_links_json TEXT NOT NULL,
    article_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (digest_key, article_key, content_hash)
)
"""

CARD_DELIVERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS card_deliveries (
    delivery_id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES article_snapshots(snapshot_id),
    article_key TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('calibration', 'personalized')),
    message_id TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (snapshot_id, purpose)
)
"""

FEEDBACK_REVISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback_revisions (
    revision_id INTEGER PRIMARY KEY,
    delivery_id INTEGER NOT NULL REFERENCES card_deliveries(delivery_id),
    owner_open_id TEXT NOT NULL,
    sentiment TEXT NOT NULL CHECK (sentiment IN ('like', 'dislike')),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

OWNER_SCHEMA = """
CREATE TABLE IF NOT EXISTS scout_owner (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    open_id TEXT NOT NULL UNIQUE,
    bound_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""

PREFERENCE_PROFILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS preference_profiles (
    version INTEGER PRIMARY KEY,
    parent_version INTEGER,
    profile_json TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    last_feedback_revision_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('llm', 'rollback')),
    rollback_from_version INTEGER,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    notified_at TEXT,
    notification_message_id TEXT,
    notification_chat_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

EVALUATION_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluation_cache (
    article_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (article_key, content_hash, profile_version)
)
"""


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


class FeedbackError(ValueError):
    """Raised when a callback is invalid or belongs to a different owner."""


class SQLiteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        """Create every state table, failing fast on a legacy schema."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._initialize_delivered(connection)
                connection.execute(SOURCE_STATE_SCHEMA)
                connection.execute(BASELINE_SCHEMA)
                connection.execute(ACTIVE_FAILURES_SCHEMA)
                connection.execute(ARTICLE_SNAPSHOTS_SCHEMA)
                connection.execute(CARD_DELIVERIES_SCHEMA)
                connection.execute(FEEDBACK_REVISIONS_SCHEMA)
                connection.execute(OWNER_SCHEMA)
                connection.execute(PREFERENCE_PROFILES_SCHEMA)
                connection.execute(EVALUATION_CACHE_SCHEMA)
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS delivered_items_dedupe_key_idx
                    ON delivered_items (dedupe_key)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS article_snapshots_article_key_idx
                    ON article_snapshots (article_key)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS card_deliveries_article_key_idx
                    ON card_deliveries (article_key)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS feedback_revisions_delivery_idx
                    ON feedback_revisions (delivery_id, revision_id DESC)
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS preference_profiles_active_idx
                    ON preference_profiles (active) WHERE active = 1
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS baseline_items_dedupe_key_idx
                    ON baseline_items (dedupe_key)
                    """
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def unseen(
        self, items: Iterable[NewsItem], *, read_only: bool = False
    ) -> list[NewsItem]:
        """Return items absent from both successful delivery and first baseline."""

        candidates = list(items)
        if not candidates:
            return []
        connection = self._open_for_read(read_only)
        if connection is None:
            return _deduplicate_batch(candidates)
        try:
            delivered_keys, delivered_ids, delivered_urls = _delivered_state(connection)
            baseline_keys = _baseline_keys(connection)
            # The baseline keeps the stable identities (feed GUID, CMS id, and
            # first-seen URL) of a source's opening window.  Joining on them
            # here keeps a relisted article unseen-blocked even when the site
            # later changes its URL shape (for example a locale prefix).
            baseline_ids, baseline_urls = _baseline_state(connection)
            seen_dedupe_keys = delivered_keys | baseline_keys
            seen_item_ids = delivered_ids | baseline_ids
            seen_urls = delivered_urls | baseline_urls
            result: list[NewsItem] = []
            for item in candidates:
                dedupe_key = _item_dedupe_key(item)
                item_id_key = (item.source, item.item_id)
                url_key = (item.source, item.url)
                is_ordinary_article = dedupe_key == canonicalize_url(item.url)
                if dedupe_key in seen_dedupe_keys or (
                    is_ordinary_article
                    and (item_id_key in seen_item_ids or url_key in seen_urls)
                ):
                    continue
                result.append(item)
                seen_dedupe_keys.add(dedupe_key)
                seen_item_ids.add(item_id_key)
                if is_ordinary_article:
                    seen_urls.add(url_key)
            return result
        finally:
            connection.close()

    def is_delivered(self, item: NewsItem, *, read_only: bool = False) -> bool:
        connection = self._open_for_read(read_only)
        if connection is None:
            return False
        try:
            delivered_keys, delivered_ids, delivered_urls = _delivered_state(connection)
            baseline_ids, baseline_urls = _baseline_state(connection)
            dedupe_key = _item_dedupe_key(item)
            is_ordinary_article = dedupe_key == canonicalize_url(item.url)
            return dedupe_key in delivered_keys or (
                is_ordinary_article
                and (
                    (item.source, item.item_id) in delivered_ids | baseline_ids
                    or (item.source, item.url) in delivered_urls | baseline_urls
                )
            )
        finally:
            connection.close()

    def record_delivered(self, items: Iterable[NewsItem]) -> None:
        delivered = list(items)
        if not delivered:
            return
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            for item in delivered:
                dedupe_key = _item_dedupe_key(item)
                connection.execute(
                    """
                    INSERT INTO delivered_items (source, item_id, url, dedupe_key)
                    SELECT ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM delivered_items WHERE dedupe_key = ?
                    )
                    """,
                    (item.source, item.item_id, item.url, dedupe_key, dedupe_key),
                )
            # Delivery is the recovery boundary for article-level failures.
            connection.executemany(
                "DELETE FROM active_failures WHERE source = ? AND item_key = ?",
                ((item.source, _item_dedupe_key(item)) for item in delivered),
            )

    def is_source_initialized(self, source: str, *, read_only: bool = False) -> bool:
        connection = self._open_for_read(read_only)
        if connection is None:
            return False
        try:
            if _table_exists(connection, "source_state"):
                row = connection.execute(
                    "SELECT 1 FROM source_state WHERE source = ? LIMIT 1", (source,)
                ).fetchone()
                if row is not None:
                    return True
            return False
        finally:
            connection.close()

    def initialize_source_baseline(
        self, source: str, items: Iterable[NewsItem]
    ) -> bool:
        """Atomically save a source's first window and mark it initialized.

        Returns ``True`` when this call established the baseline and ``False``
        when another run had already initialized the source.
        """

        baseline = list(items)
        if not source:
            raise ValueError("source must not be empty")
        if any(item.source != source for item in baseline):
            raise ValueError("all baseline items must belong to the source")

        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM source_state WHERE source = ? LIMIT 1", (source,)
                ).fetchone():
                    connection.rollback()
                    return False
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO baseline_items
                        (source, dedupe_key, item_id, url)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        (source, _item_dedupe_key(item), item.item_id, item.url)
                        for item in baseline
                    ),
                )
                connection.execute(
                    "INSERT INTO source_state (source) VALUES (?)", (source,)
                )
                # Establishing the no-backfill baseline is the successful
                # terminal state for a previously malformed historical entry.
                connection.executemany(
                    "DELETE FROM active_failures WHERE source = ? AND item_key = ?",
                    ((source, _item_dedupe_key(item)) for item in baseline),
                )
                connection.execute(
                    "DELETE FROM active_failures WHERE source = ? AND item_key = ?",
                    (source, BASELINE_FAILURE_ITEM_KEY),
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                return True

    def baseline_items(self, source: str, *, read_only: bool = False) -> frozenset[str]:
        """Return every dedupe key captured in a source's first window."""

        connection = self._open_for_read(read_only)
        if connection is None:
            return frozenset()
        try:
            if not _table_exists(connection, "baseline_items"):
                return frozenset()
            return frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT dedupe_key FROM baseline_items WHERE source = ?", (source,)
                )
            )
        finally:
            connection.close()

    def is_baseline_item(self, item: NewsItem, *, read_only: bool = False) -> bool:
        connection = self._open_for_read(read_only)
        if connection is None:
            return False
        try:
            return _item_dedupe_key(item) in _baseline_keys(connection)
        finally:
            connection.close()

    def has_active_failure(
        self, source: str, item_key: str, *, read_only: bool = False
    ) -> bool:
        connection = self._open_for_read(read_only)
        if connection is None:
            return False
        try:
            if not _table_exists(connection, "active_failures"):
                return False
            return (
                connection.execute(
                    """
                    SELECT 1 FROM active_failures
                    WHERE source = ? AND item_key = ? LIMIT 1
                    """,
                    (source, item_key),
                ).fetchone()
                is not None
            )
        finally:
            connection.close()

    def record_active_failure(self, source: str, item_key: str) -> bool:
        """Record a successfully alerted failure, returning whether it was new."""

        if not source or not item_key:
            raise ValueError("source and item_key must not be empty")
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO active_failures (source, item_key)
                VALUES (?, ?)
                """,
                (source, item_key),
            )
            return cursor.rowcount == 1

    def clear_active_failure(self, source: str, item_key: str | None = None) -> int:
        """Clear one recovered event, or all active events for a source."""

        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            if item_key is None:
                cursor = connection.execute(
                    "DELETE FROM active_failures WHERE source = ?", (source,)
                )
            else:
                cursor = connection.execute(
                    """
                    DELETE FROM active_failures
                    WHERE source = ? AND item_key = ?
                    """,
                    (source, item_key),
                )
            return cursor.rowcount

    def clear_active_failure_keys(self, source: str, item_keys: Iterable[str]) -> int:
        """Atomically clear a known set of recovered article events."""

        keys = {key for key in item_keys if key}
        if not source:
            raise ValueError("source must not be empty")
        if not keys:
            return 0
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            before = connection.total_changes
            connection.executemany(
                "DELETE FROM active_failures WHERE source = ? AND item_key = ?",
                ((source, key) for key in keys),
            )
            return connection.total_changes - before

    def clear_active_failures_with_prefix(self, source: str, prefix: str) -> int:
        """Clear recovered internal event keys in one namespace.

        Prefixes are generated by Scout itself, not accepted from SQL, so
        escaping the LIKE wildcards makes the operation exact and predictable.
        """

        if not source or not prefix:
            raise ValueError("source and prefix must not be empty")
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            cursor = connection.execute(
                """
                DELETE FROM active_failures
                WHERE source = ? AND item_key LIKE ? ESCAPE '\\'
                """,
                (source, escaped + "%"),
            )
            return cursor.rowcount

    def reconcile_active_failure_namespace(
        self, source: str, prefix: str, active_keys: Iterable[str]
    ) -> int:
        """Clear namespaced failures that disappeared from the latest batch."""

        current = set(active_keys)
        if not source or not prefix:
            raise ValueError("source and prefix must not be empty")
        if any(not key.startswith(prefix) for key in current):
            raise ValueError("active failure key is outside the requested namespace")
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            stored = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT item_key FROM active_failures
                    WHERE source = ? AND item_key LIKE ? ESCAPE '\\'
                    """,
                    (source, escaped + "%"),
                )
            }
            stale = stored - current
            connection.executemany(
                "DELETE FROM active_failures WHERE source = ? AND item_key = ?",
                ((source, key) for key in stale),
            )
            return len(stale)

    # -- Personalized article snapshots and delivery ---------------------

    def save_article_snapshots(
        self, articles: Iterable[DigestArticle]
    ) -> dict[tuple[str, str], int]:
        snapshots = list(articles)
        if not snapshots:
            return {}
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for article in snapshots:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO article_snapshots (
                            digest_key, position, number, category, title,
                            article_url, summary, detail, related_links_json,
                            article_key, content_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            article.digest_key,
                            article.position,
                            article.number,
                            article.category,
                            article.title,
                            article.article_url,
                            article.summary,
                            article.detail,
                            json.dumps(article.related_links, ensure_ascii=False),
                            article.article_key,
                            article.content_hash,
                        ),
                    )
                digest_keys = tuple(
                    dict.fromkeys(article.digest_key for article in snapshots)
                )
                rows = connection.execute(
                    f"""
                    SELECT snapshot_id, article_key, content_hash
                    FROM article_snapshots
                    WHERE digest_key IN ({",".join("?" for _ in digest_keys)})
                    """,
                    digest_keys,
                ).fetchall()
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
        wanted = {(article.article_key, article.content_hash) for article in snapshots}
        return {
            (row[1], row[2]): int(row[0]) for row in rows if (row[1], row[2]) in wanted
        }

    def is_article_delivered(
        self, article_key: str, *, read_only: bool = False
    ) -> bool:
        connection = self._open_for_read(read_only)
        if connection is None:
            return False
        try:
            if not _table_exists(connection, "card_deliveries"):
                return False
            return (
                connection.execute(
                    "SELECT 1 FROM card_deliveries WHERE article_key = ? LIMIT 1",
                    (article_key,),
                ).fetchone()
                is not None
            )
        finally:
            connection.close()

    def is_snapshot_delivered(
        self, snapshot_id: int, purpose: str, *, read_only: bool = False
    ) -> bool:
        connection = self._open_for_read(read_only)
        if connection is None:
            return False
        try:
            if not _table_exists(connection, "card_deliveries"):
                return False
            return (
                connection.execute(
                    """
                    SELECT 1 FROM card_deliveries
                    WHERE snapshot_id = ? AND purpose = ? LIMIT 1
                    """,
                    (snapshot_id, purpose),
                ).fetchone()
                is not None
            )
        finally:
            connection.close()

    def all_articles_delivered(
        self, article_keys: Iterable[str], *, read_only: bool = False
    ) -> bool:
        keys = tuple(dict.fromkeys(article_keys))
        if not keys:
            return False
        connection = self._open_for_read(read_only)
        if connection is None:
            return False
        try:
            if not _table_exists(connection, "card_deliveries"):
                return False
            placeholders = ",".join("?" for _ in keys)
            count = connection.execute(
                f"""
                SELECT count(DISTINCT article_key) FROM card_deliveries
                WHERE article_key IN ({placeholders})
                """,
                keys,
            ).fetchone()[0]
            return int(count) == len(keys)
        finally:
            connection.close()

    def record_card_delivery(
        self,
        *,
        snapshot_id: int,
        article_key: str,
        purpose: str,
        message_id: str,
        chat_id: str,
        evaluation: PersonalizedEvaluation,
    ) -> int:
        if purpose not in {"calibration", "personalized"}:
            raise ValueError("unsupported card delivery purpose")
        self.initialize()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO card_deliveries (
                    snapshot_id, article_key, purpose, message_id, chat_id,
                    verdict, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id, purpose) DO NOTHING
                """,
                (
                    snapshot_id,
                    article_key,
                    purpose,
                    message_id,
                    chat_id,
                    evaluation.verdict,
                    evaluation.reason,
                ),
            )
            if cursor.rowcount == 1:
                return int(cursor.lastrowid)
            row = connection.execute(
                """
                SELECT delivery_id FROM card_deliveries
                WHERE snapshot_id = ? AND purpose = ?
                """,
                (snapshot_id, purpose),
            ).fetchone()
            if row is None:
                raise StorageError("card delivery could not be recorded")
            return int(row[0])

    def get_card_delivery(
        self, delivery_id: int, *, read_only: bool = False
    ) -> CardDelivery | None:
        connection = self._open_for_read(read_only)
        if connection is None:
            return None
        try:
            if not _table_exists(connection, "card_deliveries"):
                return None
            row = connection.execute(
                """
                SELECT d.delivery_id, d.snapshot_id, d.purpose, d.message_id,
                       d.chat_id, d.verdict, d.reason,
                       s.digest_key, s.position, s.number, s.category, s.title,
                       s.article_url, s.summary, s.detail, s.related_links_json,
                       s.article_key, s.content_hash
                FROM card_deliveries d
                JOIN article_snapshots s ON s.snapshot_id = d.snapshot_id
                WHERE d.delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
            return _card_delivery(row) if row is not None else None
        finally:
            connection.close()

    def get_card_delivery_by_snapshot(
        self,
        snapshot_id: int,
        purpose: str,
        *,
        read_only: bool = False,
    ) -> CardDelivery | None:
        connection = self._open_for_read(read_only)
        if connection is None:
            return None
        try:
            if not _table_exists(connection, "card_deliveries"):
                return None
            row = connection.execute(
                """
                SELECT d.delivery_id, d.snapshot_id, d.purpose, d.message_id,
                       d.chat_id, d.verdict, d.reason,
                       s.digest_key, s.position, s.number, s.category, s.title,
                       s.article_url, s.summary, s.detail, s.related_links_json,
                       s.article_key, s.content_hash
                FROM card_deliveries d
                JOIN article_snapshots s ON s.snapshot_id = d.snapshot_id
                WHERE d.snapshot_id = ? AND d.purpose = ?
                """,
                (snapshot_id, purpose),
            ).fetchone()
            return _card_delivery(row) if row is not None else None
        finally:
            connection.close()

    # -- Feedback revisions and sole owner -------------------------------

    def record_feedback(
        self,
        *,
        delivery_id: int,
        message_id: str,
        open_id: str,
        sentiment: str,
        reason: str,
    ) -> FeedbackWrite:
        reason = reason.strip()
        if sentiment not in {"like", "dislike"}:
            raise FeedbackError("反馈倾向无效")
        if not 1 <= len(reason) <= 500:
            raise FeedbackError("反馈原因必须为 1–500 字")
        if not open_id:
            raise FeedbackError("回调缺少 open_id")
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                delivery = connection.execute(
                    """
                    SELECT 1 FROM card_deliveries
                    WHERE delivery_id = ? AND message_id = ?
                    """,
                    (delivery_id, message_id),
                ).fetchone()
                if delivery is None:
                    raise FeedbackError("反馈卡片与送达记录不匹配")
                owner = connection.execute(
                    "SELECT open_id FROM scout_owner WHERE singleton = 1"
                ).fetchone()
                if owner is None:
                    connection.execute(
                        "INSERT INTO scout_owner (singleton, open_id) VALUES (1, ?)",
                        (open_id,),
                    )
                elif owner[0] != open_id:
                    raise FeedbackError("该 Scout 已绑定其他 owner")
                latest = connection.execute(
                    """
                    SELECT revision_id, sentiment, reason
                    FROM feedback_revisions
                    WHERE delivery_id = ? ORDER BY revision_id DESC LIMIT 1
                    """,
                    (delivery_id,),
                ).fetchone()
                if (
                    latest is not None
                    and latest[1] == sentiment
                    and latest[2] == reason
                ):
                    connection.commit()
                    return FeedbackWrite(int(latest[0]), False, sentiment, reason)
                cursor = connection.execute(
                    """
                    INSERT INTO feedback_revisions (
                        delivery_id, owner_open_id, sentiment, reason
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (delivery_id, open_id, sentiment, reason),
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                return FeedbackWrite(int(cursor.lastrowid), True, sentiment, reason)

    def latest_feedback_for_delivery(
        self, delivery_id: int, *, read_only: bool = False
    ) -> FeedbackEvidence | None:
        evidence = self.feedback_evidence(read_only=read_only)
        return next(
            (item for item in evidence if item.delivery_id == delivery_id), None
        )

    def feedback_evidence(
        self, *, read_only: bool = False
    ) -> tuple[FeedbackEvidence, ...]:
        connection = self._open_for_read(read_only)
        if connection is None:
            return ()
        try:
            if not _table_exists(connection, "feedback_revisions"):
                return ()
            rows = connection.execute(
                """
                SELECT f.revision_id, f.delivery_id, s.article_key, f.sentiment,
                       f.reason, s.title, s.category, s.summary, s.detail,
                       f.created_at
                FROM feedback_revisions f
                JOIN card_deliveries d ON d.delivery_id = f.delivery_id
                JOIN article_snapshots s ON s.snapshot_id = d.snapshot_id
                WHERE f.revision_id = (
                    SELECT max(f2.revision_id) FROM feedback_revisions f2
                    WHERE f2.delivery_id = f.delivery_id
                )
                ORDER BY f.revision_id
                """
            ).fetchall()
            return tuple(FeedbackEvidence(*row) for row in rows)
        finally:
            connection.close()

    def recent_feedback(
        self, sentiment: str, *, limit: int = 4, read_only: bool = False
    ) -> tuple[FeedbackEvidence, ...]:
        values = [
            item
            for item in reversed(self.feedback_evidence(read_only=read_only))
            if item.sentiment == sentiment
        ]
        return tuple(values[:limit])

    def max_feedback_revision_id(self, *, read_only: bool = False) -> int:
        connection = self._open_for_read(read_only)
        if connection is None:
            return 0
        try:
            if not _table_exists(connection, "feedback_revisions"):
                return 0
            return int(
                connection.execute(
                    "SELECT coalesce(max(revision_id), 0) FROM feedback_revisions"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    # -- Preference profiles ---------------------------------------------

    def active_profile(self, *, read_only: bool = False) -> PreferenceProfile | None:
        connection = self._open_for_read(read_only)
        if connection is None:
            return None
        try:
            if not _table_exists(connection, "preference_profiles"):
                return None
            row = connection.execute(
                """
                SELECT version, profile_json, evidence_ids_json, change_summary,
                       last_feedback_revision_id, notified_at
                FROM preference_profiles WHERE active = 1
                """
            ).fetchone()
            return _preference_profile(row) if row is not None else None
        finally:
            connection.close()

    def profile_history(
        self, *, read_only: bool = False
    ) -> tuple[dict[str, object], ...]:
        connection = self._open_for_read(read_only)
        if connection is None:
            return ()
        try:
            if not _table_exists(connection, "preference_profiles"):
                return ()
            rows = connection.execute(
                """
                SELECT version, parent_version, source, rollback_from_version,
                       active, change_summary, last_feedback_revision_id,
                       notified_at, created_at
                FROM preference_profiles ORDER BY version DESC
                """
            ).fetchall()
            keys = (
                "version",
                "parent_version",
                "source",
                "rollback_from_version",
                "active",
                "change_summary",
                "last_feedback_revision_id",
                "notified_at",
                "created_at",
            )
            return tuple(dict(zip(keys, row, strict=True)) for row in rows)
        finally:
            connection.close()

    def save_profile(
        self, profile: PreferenceProfile, *, source: str = "llm"
    ) -> PreferenceProfile:
        if source not in {"llm", "rollback"}:
            raise ValueError("invalid profile source")
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT version FROM preference_profiles WHERE active = 1"
                ).fetchone()
                parent_version = int(row[0]) if row is not None else None
                version = int(
                    connection.execute(
                        "SELECT coalesce(max(version), 0) + 1 FROM preference_profiles"
                    ).fetchone()[0]
                )
                connection.execute("UPDATE preference_profiles SET active = 0")
                saved = replace(profile, version=version, notified=False)
                connection.execute(
                    """
                    INSERT INTO preference_profiles (
                        version, parent_version, profile_json, evidence_ids_json,
                        change_summary, last_feedback_revision_id, source, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        version,
                        parent_version,
                        json.dumps(saved.as_prompt_dict(), ensure_ascii=False),
                        json.dumps(saved.evidence_ids),
                        saved.change_summary,
                        saved.last_feedback_revision_id,
                        source,
                    ),
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                return saved

    def rollback_profile(self, target_version: int) -> PreferenceProfile:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                target = connection.execute(
                    """
                    SELECT profile_json, evidence_ids_json FROM preference_profiles
                    WHERE version = ?
                    """,
                    (target_version,),
                ).fetchone()
                if target is None:
                    raise StorageError(
                        f"preference profile v{target_version} not found"
                    )
                active = connection.execute(
                    "SELECT version FROM preference_profiles WHERE active = 1"
                ).fetchone()
                parent_version = int(active[0]) if active is not None else None
                version = int(
                    connection.execute(
                        "SELECT coalesce(max(version), 0) + 1 FROM preference_profiles"
                    ).fetchone()[0]
                )
                latest_feedback = int(
                    connection.execute(
                        "SELECT coalesce(max(revision_id), 0) FROM feedback_revisions"
                    ).fetchone()[0]
                )
                payload = json.loads(target[0])
                payload["version"] = version
                summary = f"回滚到 v{target_version}；现有反馈已标记为已处理"
                payload["change_summary"] = summary
                connection.execute("UPDATE preference_profiles SET active = 0")
                connection.execute(
                    """
                    INSERT INTO preference_profiles (
                        version, parent_version, profile_json, evidence_ids_json,
                        change_summary, last_feedback_revision_id, source,
                        rollback_from_version, active
                    ) VALUES (?, ?, ?, ?, ?, ?, 'rollback', ?, 1)
                    """,
                    (
                        version,
                        parent_version,
                        json.dumps(payload, ensure_ascii=False),
                        target[1],
                        summary,
                        latest_feedback,
                        target_version,
                    ),
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
        profile = self.active_profile(read_only=True)
        if profile is None:
            raise StorageError("rollback did not create an active profile")
        return profile

    def mark_profile_notified(
        self, version: int, *, message_id: str, chat_id: str
    ) -> None:
        self.initialize()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE preference_profiles
                SET notified_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                    notification_message_id = ?, notification_chat_id = ?
                WHERE version = ? AND active = 1
                """,
                (message_id, chat_id, version),
            )
            if cursor.rowcount != 1:
                raise StorageError(
                    "active preference profile changed before notification"
                )

    # -- Evaluation cache -------------------------------------------------

    def cached_evaluation(
        self,
        article: DigestArticle,
        profile_version: int,
        *,
        read_only: bool = False,
    ) -> PersonalizedEvaluation | None:
        connection = self._open_for_read(read_only)
        if connection is None:
            return None
        try:
            if not _table_exists(connection, "evaluation_cache"):
                return None
            row = connection.execute(
                """
                SELECT verdict, reason FROM evaluation_cache
                WHERE article_key = ? AND content_hash = ? AND profile_version = ?
                """,
                (article.article_key, article.content_hash, profile_version),
            ).fetchone()
            if row is None:
                return None
            return PersonalizedEvaluation(article.article_key, row[0], row[1])
        finally:
            connection.close()

    def save_evaluations(
        self,
        articles: Iterable[DigestArticle],
        evaluations: Iterable[PersonalizedEvaluation],
        *,
        profile_version: int,
    ) -> None:
        article_map = {article.article_key: article for article in articles}
        values = list(evaluations)
        if not values:
            return
        self.initialize()
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO evaluation_cache (
                    article_key, content_hash, profile_version, verdict, reason
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        evaluation.article_key,
                        article_map[evaluation.article_key].content_hash,
                        profile_version,
                        evaluation.verdict,
                        evaluation.reason,
                    )
                    for evaluation in values
                ),
            )

    def _initialize_delivered(self, connection: sqlite3.Connection) -> None:
        if not _table_exists(connection, "delivered_items"):
            connection.execute(DELIVERED_SCHEMA)
            return

        columns = _table_columns(connection, "delivered_items")
        if "delivery_id" not in columns or "dedupe_key" not in columns:
            raise StorageError(
                "delivered_items has a legacy schema; run a current release once "
                "to migrate it before removing migration support"
            )
        if connection.execute(
            "SELECT 1 FROM delivered_items WHERE dedupe_key IS NULL LIMIT 1"
        ).fetchone():
            raise StorageError("delivered_items contains rows with a NULL dedupe_key")

    def _open_for_read(self, read_only: bool) -> sqlite3.Connection | None:
        if read_only:
            if not self.path.exists():
                return None
            return self._connect_read_only()
        self.initialize()
        return self._connect()

    def _connect_read_only(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5
        )
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _delivered_state(
    connection: sqlite3.Connection,
) -> tuple[set[str], set[tuple[str, str]], set[tuple[str, str]]]:
    if not _table_exists(connection, "delivered_items"):
        return set(), set(), set()
    rows = connection.execute(
        "SELECT source, item_id, url, dedupe_key FROM delivered_items"
    ).fetchall()
    keys: set[str] = set()
    ids: set[tuple[str, str]] = set()
    urls: set[tuple[str, str]] = set()
    for source, item_id, url, dedupe_key in rows:
        keys.add(dedupe_key)
        ids.add((source, item_id))
        urls.add((source, url))
    return keys, ids, urls


def _baseline_keys(connection: sqlite3.Connection) -> set[str]:
    if not _table_exists(connection, "baseline_items"):
        return set()
    return {
        row[0] for row in connection.execute("SELECT dedupe_key FROM baseline_items")
    }


def _baseline_state(
    connection: sqlite3.Connection,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Return the (source, item_id) and (source, url) pairs of first baselines."""

    if not _table_exists(connection, "baseline_items"):
        return set(), set()
    ids: set[tuple[str, str]] = set()
    urls: set[tuple[str, str]] = set()
    for source, item_id, url in connection.execute(
        "SELECT source, item_id, url FROM baseline_items"
    ):
        if item_id:
            ids.add((source, item_id))
        if url:
            urls.add((source, url))
    return ids, urls


def _deduplicate_batch(items: list[NewsItem]) -> list[NewsItem]:
    result: list[NewsItem] = []
    seen_keys: set[str] = set()
    seen_ids: set[tuple[str, str]] = set()
    seen_urls: set[tuple[str, str]] = set()
    for item in items:
        dedupe_key = _item_dedupe_key(item)
        item_id_key = (item.source, item.item_id)
        url_key = (item.source, item.url)
        ordinary = dedupe_key == canonicalize_url(item.url)
        if dedupe_key in seen_keys or (
            ordinary and (item_id_key in seen_ids or url_key in seen_urls)
        ):
            continue
        result.append(item)
        seen_keys.add(dedupe_key)
        seen_ids.add(item_id_key)
        if ordinary:
            seen_urls.add(url_key)
    return result


def _item_dedupe_key(item: NewsItem) -> str:
    return item.dedupe_key


def _card_delivery(row: sqlite3.Row | tuple[object, ...]) -> CardDelivery:
    related_raw = json.loads(str(row[15]))
    related_links = tuple((str(link[0]), str(link[1])) for link in related_raw)
    article = DigestArticle(
        digest_key=str(row[7]),
        position=int(row[8]),
        number=str(row[9]),
        category=str(row[10]),
        title=str(row[11]),
        article_url=str(row[12]),
        summary=str(row[13]),
        detail=str(row[14]),
        related_links=related_links,
        article_key=str(row[16]),
        content_hash=str(row[17]),
    )
    return CardDelivery(
        delivery_id=int(row[0]),
        snapshot_id=int(row[1]),
        article=article,
        purpose=str(row[2]),
        message_id=str(row[3]),
        chat_id=str(row[4]),
        verdict=str(row[5]),
        reason=str(row[6]),
    )


def _preference_profile(row: sqlite3.Row | tuple[object, ...]) -> PreferenceProfile:
    payload = json.loads(str(row[1]))
    evidence_ids = tuple(int(value) for value in json.loads(str(row[2])))
    return PreferenceProfile(
        version=int(row[0]),
        like_rules=tuple(str(value) for value in payload.get("like_rules", [])),
        dislike_rules=tuple(str(value) for value in payload.get("dislike_rules", [])),
        tradeoffs=tuple(str(value) for value in payload.get("tradeoffs", [])),
        uncertainties=tuple(str(value) for value in payload.get("uncertainties", [])),
        evidence_ids=evidence_ids,
        change_summary=str(row[3]),
        last_feedback_revision_id=int(row[4]),
        notified=row[5] is not None,
    )
