"""SQLite-backed delivery, article, feedback, and preference state."""

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from .database import connect, transaction
from .issue_state import ISSUE_SCHEMAS
from .locking import sender_lock
from .model import (
    PROFILE_CATEGORIES,
    PROFILE_FORMAT_VERSION,
    CardDelivery,
    DigestArticle,
    FeedbackEvidence,
    FeedbackWrite,
    NewsItem,
    PersonalizedEvaluation,
    PreferenceEntry,
    PreferenceProfile,
    PreferenceUpdate,
    ProfileSnapshot,
    canonicalize_url,
)


class StorageError(RuntimeError):
    """Raised when the SQLite database has an unsupported legacy schema."""


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

FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    delivery_id INTEGER PRIMARY KEY REFERENCES card_deliveries(delivery_id),
    owner_open_id TEXT NOT NULL,
    sentiment TEXT NOT NULL CHECK (sentiment IN ('like', 'dislike')),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    created_seq INTEGER NOT NULL CHECK (created_seq > 0),
    change_seq INTEGER NOT NULL UNIQUE CHECK (change_seq >= created_seq),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

FEEDBACK_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    change_seq INTEGER NOT NULL CHECK (change_seq >= 0)
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
    last_feedback_change_seq INTEGER NOT NULL,
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

PREFERENCE_DETAIL_SCHEMAS = (
    """CREATE TABLE IF NOT EXISTS preference_entries (
        profile_version INTEGER NOT NULL REFERENCES preference_profiles(version),
        entry_id TEXT NOT NULL,
        category TEXT NOT NULL CHECK (category IN ('rules', 'entities', 'interests', 'questions')),
        position INTEGER NOT NULL,
        text TEXT NOT NULL,
        PRIMARY KEY (profile_version, entry_id),
        UNIQUE (profile_version, category, position)
    )""",
    """CREATE TABLE IF NOT EXISTS preference_entry_evidence (
        profile_version INTEGER NOT NULL,
        entry_id TEXT NOT NULL,
        feedback_id INTEGER NOT NULL REFERENCES feedback(delivery_id),
        feedback_change_seq INTEGER NOT NULL CHECK (feedback_change_seq > 0),
        PRIMARY KEY (profile_version, entry_id, feedback_id),
        FOREIGN KEY (profile_version, entry_id)
            REFERENCES preference_entries(profile_version, entry_id)
    )""",
    """CREATE TABLE IF NOT EXISTS preference_updates (
        profile_version INTEGER PRIMARY KEY REFERENCES preference_profiles(version),
        mode TEXT NOT NULL CHECK (mode IN ('incremental', 'rebuild', 'rollback')),
        trigger TEXT NOT NULL,
        feedback_count INTEGER NOT NULL,
        change_count INTEGER NOT NULL,
        changes_since_rebuild INTEGER NOT NULL,
        last_full_feedback_change_seq INTEGER NOT NULL,
        input_chars INTEGER NOT NULL
    )""",
)


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
                if _table_exists(connection, "feedback_revisions"):
                    raise StorageError("feedback baseline migration is required")
                self._initialize_delivered(connection)
                connection.execute(SOURCE_STATE_SCHEMA)
                connection.execute(BASELINE_SCHEMA)
                connection.execute(ARTICLE_SNAPSHOTS_SCHEMA)
                connection.execute(CARD_DELIVERIES_SCHEMA)
                connection.execute(FEEDBACK_SCHEMA)
                connection.execute(FEEDBACK_STATE_SCHEMA)
                connection.execute("INSERT OR IGNORE INTO feedback_state VALUES (1, 0)")
                connection.execute(OWNER_SCHEMA)
                connection.execute(PREFERENCE_PROFILES_SCHEMA)
                for schema in PREFERENCE_DETAIL_SCHEMAS:
                    connection.execute(schema)
                connection.execute(EVALUATION_CACHE_SCHEMA)
                for schema in ISSUE_SCHEMAS:
                    connection.execute(schema)
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
            return _deduplicate_batch(
                candidates,
                seen_keys=delivered_keys | baseline_keys,
                seen_ids=delivered_ids | baseline_ids,
                seen_urls=delivered_urls | baseline_urls,
            )
        finally:
            connection.close()

    def record_delivered(self, items: Iterable[NewsItem]) -> None:
        delivered = list(items)
        if not delivered:
            return
        with transaction(self.path) as connection:
            for item in delivered:
                dedupe_key = item.dedupe_key
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

        with closing(self._connect()) as connection:
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
                        (source, item.dedupe_key, item.item_id, item.url)
                        for item in baseline
                    ),
                )
                connection.execute(
                    "INSERT INTO source_state (source) VALUES (?)", (source,)
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                return True

    # -- Personalized article snapshots and delivery ---------------------

    def save_article_snapshots(
        self, articles: Iterable[DigestArticle]
    ) -> dict[tuple[str, str], int]:
        snapshots = list(articles)
        if not snapshots:
            return {}
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
        with transaction(self.path) as connection:
            return self._record_card_delivery(
                connection,
                snapshot_id=snapshot_id,
                article_key=article_key,
                purpose=purpose,
                message_id=message_id,
                chat_id=chat_id,
                evaluation=evaluation,
            )

    @staticmethod
    def _record_card_delivery(
        connection: sqlite3.Connection,
        *,
        snapshot_id: int,
        article_key: str,
        purpose: str,
        message_id: str,
        chat_id: str,
        evaluation: PersonalizedEvaluation,
    ) -> int:
        """Record delivery within the caller's transaction, reusing prior success."""

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

    # -- Current feedback and sole owner ---------------------------------

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
                    SELECT delivery_id, sentiment, reason
                    FROM feedback WHERE delivery_id = ?
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
                change_seq = connection.execute(
                    "UPDATE feedback_state SET change_seq = change_seq + 1 "
                    "WHERE singleton = 1 RETURNING change_seq"
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO feedback (
                        delivery_id, owner_open_id, sentiment, reason,
                        created_seq, change_seq
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(delivery_id) DO UPDATE SET
                        sentiment = excluded.sentiment, reason = excluded.reason,
                        change_seq = excluded.change_seq,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (delivery_id, open_id, sentiment, reason, change_seq, change_seq),
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                return FeedbackWrite(delivery_id, True, sentiment, reason)

    def latest_feedback_for_delivery(
        self, delivery_id: int, *, read_only: bool = False
    ) -> FeedbackEvidence | None:
        connection = self._open_for_read(read_only)
        if connection is None:
            return None
        try:
            if not _table_exists(connection, "feedback"):
                return None
            row = connection.execute(
                """SELECT f.delivery_id, f.created_seq, f.change_seq,
                          s.article_key, f.sentiment, f.reason, s.title,
                          s.category, s.summary, s.detail, f.updated_at
                   FROM feedback f
                   JOIN card_deliveries d ON d.delivery_id = f.delivery_id
                   JOIN article_snapshots s ON s.snapshot_id = d.snapshot_id
                   WHERE f.delivery_id = ?""",
                (delivery_id,),
            ).fetchone()
            return FeedbackEvidence(*row) if row is not None else None
        finally:
            connection.close()

    def feedback_evidence(
        self, *, read_only: bool = False, cutoff: int | None = None
    ) -> tuple[FeedbackEvidence, ...]:
        connection = self._open_for_read(read_only)
        if connection is None:
            return ()
        try:
            if not _table_exists(connection, "feedback"):
                return ()
            return (
                _feedback_evidence(connection)
                if cutoff is None
                else _feedback_evidence(connection, cutoff=cutoff)
            )
        finally:
            connection.close()

    # -- Preference profiles ---------------------------------------------

    def profile_snapshot(self) -> ProfileSnapshot:
        """Read-only even on an unmigrated database; release before model I/O."""

        connection = self._open_for_read(True)
        if connection is None:
            return ProfileSnapshot()
        try:
            connection.execute("BEGIN")
            active = _read_profile(connection)
            processed = active.last_feedback_change_seq if active else 0
            full_cutoff = (
                active.update.last_full_feedback_change_seq
                if active and active.update
                else 0
            )
            next_version = 1
            rolled_back = False
            if _table_exists(connection, "preference_profiles"):
                next_version = connection.execute(
                    "SELECT coalesce(max(version), 0) + 1 FROM preference_profiles"
                ).fetchone()[0]
                source = connection.execute(
                    "SELECT source FROM preference_profiles WHERE active = 1"
                ).fetchone()
                rolled_back = source is not None and source[0] == "rollback"
            cutoff = new_count = since_full = 0
            edited = False
            feedback = ()
            if _table_exists(connection, "feedback"):
                cutoff = _feedback_change_seq(connection)
                new_count = cutoff - processed
                since_full = cutoff - full_cutoff
                feedback = _feedback_evidence(connection, cutoff=cutoff)
                edited = any(
                    f.created_seq <= processed < f.change_seq for f in feedback
                )
            return ProfileSnapshot(
                active=active,
                feedback=feedback,
                cutoff_change_seq=cutoff,
                next_version=next_version,
                new_change_count=new_count,
                changes_since_rebuild=since_full,
                edited_processed_feedback=edited,
                rolled_back=rolled_back,
            )
        finally:
            connection.close()

    def active_profile(self, *, read_only: bool = False) -> PreferenceProfile | None:
        connection = self._open_for_read(read_only)
        if connection is None:
            return None
        try:
            connection.execute("BEGIN")
            return _read_profile(connection)
        finally:
            connection.close()

    def profile_history(
        self, *, read_only: bool = False
    ) -> tuple[dict[str, object], ...]:
        connection = self._open_for_read(read_only)
        if connection is None:
            return ()
        try:
            connection.execute("BEGIN")
            if not _table_exists(connection, "preference_profiles"):
                return ()
            rows = connection.execute(
                """
                SELECT version, parent_version, source, rollback_from_version,
                       active, change_summary, last_feedback_change_seq,
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
                "last_feedback_change_seq",
                "notified_at",
                "created_at",
            )
            result = []
            for row in rows:
                profile = _read_profile(connection, version=row[0])
                assert profile is not None
                result.append(
                    {
                        **dict(zip(keys, row, strict=True)),
                        **profile.as_display_dict(),
                    }
                )
            return tuple(result)
        finally:
            connection.close()

    def save_profile(
        self, profile: PreferenceProfile, *, snapshot: ProfileSnapshot
    ) -> PreferenceProfile:
        if profile.format_version != PROFILE_FORMAT_VERSION or profile.update is None:
            raise StorageError(
                "generated profile requires format v2 and update progress"
            )
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
                expected_parent = snapshot.active.version if snapshot.active else None
                if (
                    parent_version != expected_parent
                    or version != snapshot.next_version
                ):
                    raise StorageError("active preference changed during model request")
                if profile.last_feedback_change_seq != snapshot.cutoff_change_seq:
                    raise StorageError(
                        "generated profile does not match feedback cutoff"
                    )
                # Later edits have replaced their old values. The in-memory
                # snapshot is the input; keep its cutoff so edits stay pending.
                valid_changes = {f.feedback_id: f.change_seq for f in snapshot.feedback}
                _validate_profile_evidence(profile, valid_changes)
                current_changes = dict(
                    connection.execute("SELECT delivery_id, change_seq FROM feedback")
                )
                for feedback_id, change_seq in valid_changes.items():
                    current = current_changes.get(feedback_id)
                    if current is None or (
                        current != change_seq and current <= snapshot.cutoff_change_seq
                    ):
                        raise StorageError(
                            "feedback snapshot no longer matches storage"
                        )
                outdated = sum(
                    current_changes[i] != valid_changes[i] for i in profile.evidence_ids
                )
                saved = replace(
                    profile,
                    version=version,
                    notified=False,
                    outdated_evidence_count=outdated,
                )
                _insert_profile(connection, saved, parent_version=parent_version)
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                return saved

    def rollback_profile(self, target_version: int) -> PreferenceProfile:
        with sender_lock(self.path):
            return self._rollback_profile(target_version)

    def _rollback_profile(self, target_version: int) -> PreferenceProfile:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                target = _read_profile(connection, version=target_version)
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
                latest_feedback = _feedback_change_seq(connection)
                summary = f"回滚到 v{target_version}；现有反馈已标记为已处理"
                current = _read_profile(connection)
                processed = current.last_feedback_change_seq if current else 0
                full_cutoff = (
                    current.update.last_full_feedback_change_seq
                    if current and current.update
                    else 0
                )
                change_count = latest_feedback - processed
                since_full = latest_feedback - full_cutoff
                saved = replace(
                    target,
                    version=version,
                    change_summary=summary,
                    last_feedback_change_seq=latest_feedback,
                    notified=False,
                    update=PreferenceUpdate(
                        mode="rollback",
                        trigger="manual_rollback",
                        feedback_count=0,
                        change_count=change_count,
                        changes_since_rebuild=since_full,
                        last_full_feedback_change_seq=full_cutoff,
                        input_chars=0,
                    ),
                )
                # Restore rules and evidence identities, never old feedback text.
                # The next feedback change forces a rebuild from current values.
                _insert_profile(
                    connection,
                    saved,
                    parent_version=parent_version,
                    rollback_from_version=target_version,
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
        with transaction(self.path) as connection:
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
        with transaction(self.path) as connection:
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
        return self._connect()

    def _connect_read_only(self) -> sqlite3.Connection:
        return connect(self.path, read_only=True)

    def _connect(self) -> sqlite3.Connection:
        return connect(self.path)


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


def _deduplicate_batch(
    items: list[NewsItem],
    *,
    seen_keys: set[str] | None = None,
    seen_ids: set[tuple[str, str]] | None = None,
    seen_urls: set[tuple[str, str]] | None = None,
) -> list[NewsItem]:
    result: list[NewsItem] = []
    seen_keys = set() if seen_keys is None else seen_keys
    seen_ids = set() if seen_ids is None else seen_ids
    seen_urls = set() if seen_urls is None else seen_urls
    for item in items:
        dedupe_key = item.dedupe_key
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


def _feedback_change_seq(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT change_seq FROM feedback_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise StorageError("feedback progress state is missing")
    return int(row[0])


def _feedback_evidence(
    connection: sqlite3.Connection, *, cutoff: int = 9_223_372_036_854_775_807
) -> tuple[FeedbackEvidence, ...]:
    rows = connection.execute(
        """SELECT f.delivery_id, f.created_seq, f.change_seq,
                  s.article_key, f.sentiment, f.reason, s.title,
                  s.category, s.summary, s.detail, f.updated_at
           FROM feedback f
           JOIN card_deliveries d ON d.delivery_id = f.delivery_id
           JOIN article_snapshots s ON s.snapshot_id = d.snapshot_id
           WHERE f.change_seq <= ? ORDER BY f.change_seq""",
        (cutoff,),
    ).fetchall()
    return tuple(FeedbackEvidence(*row) for row in rows)


def _read_profile(
    connection: sqlite3.Connection, *, version: int | None = None
) -> PreferenceProfile | None:
    if not _table_exists(connection, "preference_profiles"):
        return None
    clause = "active = 1" if version is None else "version = ?"
    row = connection.execute(
        """SELECT version, profile_json, evidence_ids_json, change_summary,
                  last_feedback_change_seq, notified_at
           FROM preference_profiles WHERE """
        + clause,
        () if version is None else (version,),
    ).fetchone()
    return _preference_profile(connection, row) if row is not None else None


def _preference_profile(
    connection: sqlite3.Connection, row: sqlite3.Row | tuple[object, ...]
) -> PreferenceProfile:
    payload = json.loads(str(row[1]))
    evidence_ids = tuple(int(value) for value in json.loads(str(row[2])))
    format_version = payload.get("format_version", 1)
    entries = ()
    if format_version == PROFILE_FORMAT_VERSION:
        if not _table_exists(connection, "preference_entries"):
            raise StorageError("format v2 preference entries are missing")
        evidence: dict[str, list[tuple[int, int]]] = {}
        for entry_id, feedback_id, change_seq in connection.execute(
            """SELECT entry_id, feedback_id, feedback_change_seq
               FROM preference_entry_evidence
               WHERE profile_version = ? ORDER BY feedback_id""",
            (row[0],),
        ):
            evidence.setdefault(entry_id, []).append((feedback_id, change_seq))
        entries = tuple(
            PreferenceEntry(
                entry_id,
                category,
                text,
                tuple(i for i, _ in evidence.get(entry_id, ())),
                tuple(evidence.get(entry_id, ())),
            )
            for entry_id, category, text in connection.execute(
                """SELECT entry_id, category, text FROM preference_entries
                   WHERE profile_version = ? ORDER BY position""",
                (row[0],),
            )
        )
    elif format_version != 1:
        raise StorageError(f"unsupported preference format: {format_version}")
    update = None
    if _table_exists(connection, "preference_updates"):
        update_row = connection.execute(
            """SELECT mode, trigger, feedback_count, change_count,
                      changes_since_rebuild, last_full_feedback_change_seq,
                      input_chars FROM preference_updates WHERE profile_version = ?""",
            (row[0],),
        ).fetchone()
        if update_row is not None:
            update = PreferenceUpdate(*update_row)
    outdated_count = connection.execute(
        """SELECT count(DISTINCT e.feedback_id)
           FROM preference_entry_evidence e
           JOIN feedback f ON f.delivery_id = e.feedback_id
           WHERE e.profile_version = ? AND e.feedback_change_seq != f.change_seq""",
        (row[0],),
    ).fetchone()[0]
    return PreferenceProfile(
        version=int(row[0]),
        like_rules=tuple(str(value) for value in payload.get("like_rules", [])),
        dislike_rules=tuple(str(value) for value in payload.get("dislike_rules", [])),
        tradeoffs=tuple(str(value) for value in payload.get("tradeoffs", [])),
        uncertainties=tuple(str(value) for value in payload.get("uncertainties", [])),
        evidence_ids=evidence_ids,
        change_summary=str(row[3]),
        last_feedback_change_seq=int(row[4]),
        notified=row[5] is not None,
        format_version=format_version,
        entries=entries,
        update=update,
        outdated_evidence_count=outdated_count,
    )


def _validate_profile_evidence(
    profile: PreferenceProfile, valid_changes: dict[int, int]
) -> None:
    ids = [e.entry_id for e in profile.entries]
    if not ids or len(ids) != len(set(ids)):
        raise StorageError("preference entries are empty or have duplicate IDs")
    all_evidence = set()
    for entry in profile.entries:
        if (
            not entry.entry_id
            or entry.category not in PROFILE_CATEGORIES
            or not entry.text.strip()
            or not entry.evidence_ids
            or any(type(i) is not int for i in entry.evidence_ids)
            or not set(entry.evidence_ids) <= valid_changes.keys()
            or len(entry.evidence_changes) != len(entry.evidence_ids)
            or dict(entry.evidence_changes)
            != {i: valid_changes.get(i) for i in entry.evidence_ids}
        ):
            raise StorageError(
                "preference entry has missing, replaced or invalid evidence"
            )
        all_evidence.update(entry.evidence_ids)
    if all_evidence != set(profile.evidence_ids):
        raise StorageError("preference evidence union does not match entry evidence")


def _insert_profile(
    connection: sqlite3.Connection,
    profile: PreferenceProfile,
    *,
    parent_version: int | None,
    rollback_from_version: int | None = None,
) -> None:
    connection.execute("UPDATE preference_profiles SET active = 0 WHERE active = 1")
    connection.execute(
        """INSERT INTO preference_profiles (
               version, parent_version, profile_json, evidence_ids_json,
               change_summary, last_feedback_change_seq, source,
               rollback_from_version, active
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            profile.version,
            parent_version,
            json.dumps(profile.as_entry_dict(), ensure_ascii=False),
            json.dumps(profile.evidence_ids),
            profile.change_summary,
            profile.last_feedback_change_seq,
            "llm" if rollback_from_version is None else "rollback",
            rollback_from_version,
        ),
    )
    connection.executemany(
        "INSERT INTO preference_entries VALUES (?, ?, ?, ?, ?)",
        [
            (profile.version, e.entry_id, e.category, position, e.text)
            for position, e in enumerate(profile.entries)
        ],
    )
    connection.executemany(
        "INSERT INTO preference_entry_evidence VALUES (?, ?, ?, ?)",
        [
            (profile.version, e.entry_id, feedback_id, change_seq)
            for e in profile.entries
            for feedback_id, change_seq in e.evidence_changes
        ],
    )
    if profile.update is not None:
        update = profile.update
        connection.execute(
            """INSERT INTO preference_updates VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile.version,
                update.mode,
                update.trigger,
                update.feedback_count,
                update.change_count,
                update.changes_since_rebuild,
                update.last_full_feedback_change_seq,
                update.input_chars,
            ),
        )
