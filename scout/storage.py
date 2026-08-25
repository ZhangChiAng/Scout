"""SQLite-backed delivery, baseline, and failure state."""

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from .model import NewsItem, canonicalize_url


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


class SQLiteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        """Create every state table, failing fast on a legacy schema."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._initialize_delivered(connection)
                connection.execute(SOURCE_STATE_SCHEMA)
                connection.execute(BASELINE_SCHEMA)
                connection.execute(ACTIVE_FAILURES_SCHEMA)
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS delivered_items_dedupe_key_idx
                    ON delivered_items (dedupe_key)
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
        return sqlite3.connect(self.path)

    def _connect_read_only(self) -> sqlite3.Connection:
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)


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
