import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scout.storage import (
    BASELINE_FAILURE_ITEM_KEY,
    SOURCE_FAILURE_ITEM_KEY,
    SQLiteStorage,
    StorageError,
)
from tests.helpers import news_item


class StorageTests(unittest.TestCase):
    def test_persists_across_reopen_and_deduplicates_same_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "feed.sqlite3"
            first = news_item()
            SQLiteStorage(path).record_delivered([first])

            reopened = SQLiteStorage(path)
            self.assertEqual(reopened.unseen([first]), [])
            same_url_new_guid = news_item(
                item_id="different-guid", guid="different-guid"
            )
            self.assertEqual(reopened.unseen([same_url_new_guid]), [])
            self.assertEqual(
                reopened.unseen(
                    [
                        news_item(
                            item_id="guid-2", guid="guid-2", url="https://example.com/2"
                        )
                    ]
                ),
                [
                    news_item(
                        item_id="guid-2", guid="guid-2", url="https://example.com/2"
                    )
                ],
            )

    def test_read_only_missing_database_does_not_create_anything(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "feed.sqlite3"
            item = news_item()
            self.assertEqual(SQLiteStorage(path).unseen([item], read_only=True), [item])
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_deduplicates_repeated_ids_and_urls_within_one_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            first = news_item()
            duplicate_id = news_item(url="https://example.com/other")
            duplicate_url = news_item(item_id="other-guid", guid="other-guid")
            self.assertEqual(
                storage.unseen([first, duplicate_id, duplicate_url]),
                [first],
            )

    def test_delivery_dedupe_key_is_global_across_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            delivered = news_item(
                url="https://Example.com/news/1?utm_source=rss&edition=cn#top"
            )
            storage.record_delivered([delivered])

            duplicate = news_item(
                source="Anthropic Newsroom",
                item_id="anthropic-guid",
                guid="anthropic-guid",
                url="https://example.com/news/1?edition=cn",
            )
            self.assertTrue(storage.is_delivered(duplicate, read_only=True))
            self.assertEqual(storage.unseen([duplicate], read_only=True), [])

    def test_changelog_keys_allow_multiple_entries_on_one_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            storage = SQLiteStorage(path)
            first = news_item(
                source="API Changelog",
                item_id="same-page-id",
                guid="same-page-id",
                url="https://example.com/changelog",
                dedupe_key="changelog:first",
            )
            second = news_item(
                source="API Changelog",
                item_id="same-page-id",
                guid="same-page-id",
                url="https://example.com/changelog",
                dedupe_key="changelog:second",
            )

            self.assertEqual(storage.unseen([first, second]), [first, second])
            storage.record_delivered([first, second])

            self.assertTrue(storage.is_delivered(first, read_only=True))
            self.assertTrue(storage.is_delivered(second, read_only=True))
            with closing(sqlite3.connect(path)) as connection, connection:
                count = connection.execute(
                    "SELECT count(*) FROM delivered_items"
                ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_baseline_secondary_identity_blocks_url_shape_changes(self) -> None:
        """Replay the 2026-08-17 incident at the storage boundary.

        A baseline captured ``/blog/<slug>`` URLs with stable CMS ids; the site
        later relists identical articles under ``/en/blog/<slug>``.  Both the
        item-id and the exact URL join must keep the relisted articles seen
        even though their dedupe keys differ, while genuinely new articles
        stay unseen.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            storage = SQLiteStorage(path)
            baselined = [
                news_item(
                    source="Kimi Research",
                    item_id="kimi-k2",
                    guid="kimi-k2",
                    url="https://www.kimi.com/blog/kimi-k2",
                ),
                news_item(
                    source="Kimi Research",
                    item_id="worldvqa",
                    guid="worldvqa",
                    url="https://www.kimi.com/blog/worldvqa",
                ),
            ]
            self.assertTrue(
                storage.initialize_source_baseline("Kimi Research", baselined)
            )

            relisted_same_id = news_item(
                source="Kimi Research",
                item_id="kimi-k2",
                guid="kimi-k2",
                url="https://www.kimi.com/en/blog/kimi-k2",
            )
            relisted_same_url = news_item(
                source="Kimi Research",
                item_id="unknown-id",
                guid="unknown-id",
                url="https://www.kimi.com/blog/worldvqa",
            )
            self.assertEqual(
                storage.unseen([relisted_same_id, relisted_same_url], read_only=True),
                [],
            )

            genuinely_new = news_item(
                source="Kimi Research",
                item_id="kimi-k3",
                guid="kimi-k3",
                url="https://www.kimi.com/en/blog/kimi-k3",
            )
            self.assertEqual(
                storage.unseen([genuinely_new], read_only=True), [genuinely_new]
            )

    def test_source_baseline_is_atomic_idempotent_and_globally_seen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            storage = SQLiteStorage(path)
            items = [
                news_item(item_id=f"item-{index}", guid=f"item-{index}", url=url)
                for index, url in enumerate(
                    ("https://example.com/1", "https://example.com/2"), start=1
                )
            ]

            self.assertTrue(storage.initialize_source_baseline("OpenAI News", items))
            self.assertTrue(storage.is_source_initialized("OpenAI News"))
            self.assertEqual(
                storage.baseline_items("OpenAI News", read_only=True),
                frozenset(item.dedupe_key for item in items),
            )
            self.assertEqual(storage.unseen(items, read_only=True), [])
            cross_source_duplicate = news_item(
                source="Anthropic Newsroom",
                item_id="duplicate",
                guid="duplicate",
                url=items[0].url,
            )
            self.assertTrue(
                storage.is_baseline_item(cross_source_duplicate, read_only=True)
            )
            self.assertEqual(
                storage.unseen([cross_source_duplicate], read_only=True), []
            )

            later = news_item(
                item_id="later", guid="later", url="https://example.com/later"
            )
            self.assertFalse(storage.initialize_source_baseline("OpenAI News", [later]))
            self.assertNotIn(
                later.dedupe_key,
                storage.baseline_items("OpenAI News", read_only=True),
            )
            self.assertEqual(storage.unseen([later], read_only=True), [later])

    def test_empty_window_still_initializes_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            self.assertTrue(storage.initialize_source_baseline("Empty Source", []))
            self.assertTrue(storage.is_source_initialized("Empty Source"))
            self.assertEqual(storage.baseline_items("Empty Source"), frozenset())

    def test_baseline_atomically_recovers_item_and_baseline_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            item = news_item(source="New Source")
            storage.record_active_failure(item.source, item.dedupe_key)
            storage.record_active_failure(item.source, BASELINE_FAILURE_ITEM_KEY)

            self.assertTrue(storage.initialize_source_baseline(item.source, [item]))

            self.assertFalse(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )
            self.assertFalse(
                storage.has_active_failure(
                    item.source, BASELINE_FAILURE_ITEM_KEY, read_only=True
                )
            )

    def test_invalid_baseline_does_not_partially_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "feed.sqlite3"
            storage = SQLiteStorage(path)
            wrong_source = news_item(source="Different Source")

            with self.assertRaisesRegex(ValueError, "belong to the source"):
                storage.initialize_source_baseline("Expected Source", [wrong_source])

            self.assertFalse(path.exists())

    def test_legacy_delivered_items_schema_fails_fast(self) -> None:
        old_schema = """
        CREATE TABLE delivered_items (
            source TEXT NOT NULL,
            item_id TEXT NOT NULL,
            url TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY (source, item_id),
            UNIQUE (source, url)
        )
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(old_schema)
                connection.execute(
                    """
                    INSERT INTO delivered_items
                        (source, item_id, url, delivered_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "OpenAI News",
                        "old-guid",
                        "https://example.com/old",
                        "2025-01-02T03:04:05Z",
                    ),
                )

            with self.assertRaises(StorageError):
                SQLiteStorage(path).initialize()
            with closing(sqlite3.connect(path)) as connection:
                tables = {
                    value[0]
                    for value in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertNotIn("source_state", tables)
            self.assertFalse(
                SQLiteStorage(path).is_source_initialized("OpenAI News", read_only=True)
            )

    def test_all_read_only_state_queries_leave_missing_database_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "feed.sqlite3"
            storage = SQLiteStorage(path)
            item = news_item()

            self.assertEqual(storage.unseen([item], read_only=True), [item])
            self.assertFalse(storage.is_delivered(item, read_only=True))
            self.assertFalse(storage.is_source_initialized(item.source, read_only=True))
            self.assertEqual(
                storage.baseline_items(item.source, read_only=True), frozenset()
            )
            self.assertFalse(storage.is_baseline_item(item, read_only=True))
            self.assertFalse(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )
            self.assertFalse(path.parent.exists())

    def test_active_failure_deduplicates_until_the_event_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            storage = SQLiteStorage(path)
            item = news_item()

            self.assertTrue(storage.record_active_failure(item.source, item.dedupe_key))
            self.assertFalse(
                storage.record_active_failure(item.source, item.dedupe_key)
            )
            self.assertTrue(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )

            storage.record_active_failure(item.source, SOURCE_FAILURE_ITEM_KEY)
            self.assertEqual(
                storage.clear_active_failure(item.source, SOURCE_FAILURE_ITEM_KEY), 1
            )
            self.assertTrue(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )

            storage.record_delivered([item])
            self.assertFalse(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )
            self.assertTrue(storage.record_active_failure(item.source, item.dedupe_key))

    def test_collection_failure_namespace_can_recover_without_clearing_articles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            item = news_item()
            storage.record_active_failure(item.source, "collection:recovered-entry")
            storage.record_active_failure(item.source, "collection:still-broken")
            storage.record_active_failure(item.source, item.dedupe_key)

            self.assertEqual(
                storage.reconcile_active_failure_namespace(
                    item.source, "collection:", {"collection:still-broken"}
                ),
                1,
            )
            self.assertFalse(
                storage.has_active_failure(
                    item.source, "collection:recovered-entry", read_only=True
                )
            )
            self.assertTrue(
                storage.has_active_failure(
                    item.source, "collection:still-broken", read_only=True
                )
            )
            self.assertTrue(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )
            self.assertEqual(
                storage.clear_active_failures_with_prefix(item.source, "collection:"),
                1,
            )


if __name__ == "__main__":
    unittest.main()
