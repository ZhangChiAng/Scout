import logging
import unittest
from io import StringIO
from unittest import mock
from urllib.error import HTTPError, URLError

from scout.collector import (
    COLLECTOR_REGISTRY,
    CollectionBatch,
    CollectionError,
    RSSCollector,
    clean_html,
    collect_source,
    normalize_date,
)
from scout.config import NetworkConfig, SourceConfig
from scout.model import NewsItem, canonicalize_url
from tests.helpers import FakeResponse

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <item>
      <title>  GPT &amp; agents </title>
      <link>https://example.com/one#section</link>
      <guid>stable-one</guid>
      <description><![CDATA[<p>Hello&nbsp; <strong>world</strong>.</p><script>bad()</script>]]></description>
      <pubDate>Sun, 10 Aug 2026 12:30:00 +0800</pubDate>
      <dc:creator> Example Author </dc:creator>
      <category>Research</category><category>AI</category>
    </item>
    <item>
      <title>Broken entry</title>
      <link>https://example.com/broken</link>
      <pubDate>not a date</pubDate>
    </item>
    <item>
      <title>Fallback ID</title>
      <link>https://example.com/two#fragment</link>
      <content:encoded><![CDATA[<div>Second<br>summary</div>]]></content:encoded>
      <pubDate>Sun, 10 Aug 2026 04:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = SourceConfig("OpenAI News", "https://example.com/rss", 20)
        self.network = NetworkConfig(15.0, 5 * 1024 * 1024, "Scout/Test")

    def test_maps_rss_cleans_html_normalizes_time_and_skips_bad_item(self) -> None:
        seen: dict[str, object] = {}

        def opener(request: object, *, timeout: float) -> FakeResponse:
            seen["request"] = request
            seen["timeout"] = timeout
            return FakeResponse(RSS)

        log_output = StringIO()
        handler = logging.StreamHandler(log_output)
        logger = logging.getLogger("scout.collector")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            items = RSSCollector(self.source, self.network, opener).collect()
        finally:
            logger.removeHandler(handler)

        self.assertEqual(len(items), 2)
        first, second = items
        self.assertEqual(first.item_id, "stable-one")
        self.assertEqual(first.title, "GPT & agents")
        self.assertEqual(
            first.content,
            "<p>Hello&nbsp; <strong>world</strong>.</p><script>bad()</script>",
        )
        self.assertEqual(first.published_at, "2026-08-10T12:30:00+08:00")
        self.assertEqual(first.author, "Example Author")
        self.assertEqual(first.category, "Research, AI")
        self.assertEqual(second.item_id, "https://example.com/two")
        self.assertEqual(second.guid, "")
        self.assertEqual(second.content, "<div>Second<br>summary</div>")
        self.assertEqual(len(items.issues), 1)
        self.assertEqual(items.issues[0].title, "Broken entry")
        self.assertEqual(first.dedupe_key, "https://example.com/one")
        self.assertIn("skipping invalid RSS item 2", log_output.getvalue())
        self.assertEqual(seen["timeout"], 15.0)
        request = seen["request"]
        self.assertEqual(request.get_method(), "GET")  # type: ignore[union-attr]
        self.assertEqual(request.get_header("User-agent"), "Scout/Test")  # type: ignore[union-attr]

    def test_limits_feed_order_before_mapping(self) -> None:
        collector = RSSCollector(
            SourceConfig("Source", "https://example.com/rss", 1),
            self.network,
            lambda request, timeout: FakeResponse(RSS),
        )
        self.assertEqual([item.item_id for item in collector.collect()], ["stable-one"])

    def test_inline_rss_entries_remain_ordinary_url_deduplication_items(self) -> None:
        original = RSSCollector(
            self.source,
            self.network,
            lambda request, timeout: FakeResponse(RSS),
        ).collect()
        changed = RSSCollector(
            self.source,
            self.network,
            lambda request, timeout: FakeResponse(
                RSS.replace(b"Hello <b>world</b>.", b"Updated inline content.")
            ),
        ).collect()

        self.assertEqual(original.items[0].dedupe_key, "https://example.com/one")
        self.assertEqual(original.items[0].dedupe_key, changed.items[0].dedupe_key)

    def test_rejects_oversized_and_invalid_xml_responses(self) -> None:
        tiny = NetworkConfig(15.0, 4, "test")
        with self.assertRaisesRegex(CollectionError, "exceeds"):
            RSSCollector(
                self.source, tiny, lambda request, timeout: FakeResponse(b"12345")
            ).collect()
        with self.assertRaisesRegex(CollectionError, "invalid RSS XML"):
            RSSCollector(
                self.source,
                self.network,
                lambda request, timeout: FakeResponse(b"<rss>"),
            ).collect()

    def test_transient_fetch_failures_are_retried_once(self) -> None:
        transient_errors = (
            URLError("timed out"),
            HTTPError("https://example.com/rss", 429, "Too Many Requests", None, None),
            HTTPError(
                "https://example.com/rss", 503, "Service Unavailable", None, None
            ),
        )
        for error in transient_errors:
            with self.subTest(error=type(error).__name__):
                if isinstance(error, HTTPError):
                    self.addCleanup(error.close)
                calls: list[object] = []

                def opener(
                    request: object,
                    *,
                    timeout: float,
                    error: BaseException = error,
                    calls: list[object] = calls,
                ) -> FakeResponse:
                    calls.append(request)
                    if len(calls) == 1:
                        raise error
                    return FakeResponse(RSS)

                with mock.patch("scout.collector.time.sleep") as sleep:
                    items = RSSCollector(self.source, self.network, opener).collect()

                self.assertEqual(len(items), 2)
                self.assertEqual(len(calls), 2)
                sleep.assert_called_once_with(3.0)

    def test_permanent_fetch_failure_fails_without_retry(self) -> None:
        calls: list[object] = []
        error = HTTPError("https://example.com/rss", 404, "Not Found", None, None)
        self.addCleanup(error.close)

        def opener(request: object, *, timeout: float) -> FakeResponse:
            calls.append(request)
            raise error

        with (
            mock.patch("scout.collector.time.sleep") as sleep,
            self.assertRaisesRegex(CollectionError, "404"),
        ):
            RSSCollector(self.source, self.network, opener).collect()

        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_transient_fetch_failure_raises_after_retry(self) -> None:
        calls: list[object] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            calls.append(request)
            raise URLError("connection reset")

        with (
            mock.patch("scout.collector.time.sleep") as sleep,
            self.assertRaisesRegex(CollectionError, r"URLError.*connection reset"),
        ):
            RSSCollector(self.source, self.network, opener).collect()

        self.assertEqual(len(calls), 2)
        self.assertEqual(sleep.call_count, 1)

    def test_clean_html_and_naive_date(self) -> None:
        self.assertEqual(clean_html("<p>A&nbsp; B</p><style>hidden</style> C"), "A B C")
        self.assertEqual(
            normalize_date("10 Aug 2026 04:00:00"),
            "2026-08-10T12:00:00+08:00",
        )
        self.assertEqual(
            normalize_date("10 Aug 2026 04:00:00 -0700"),
            "2026-08-10T19:00:00+08:00",
        )

    def test_registry_covers_rss_only_and_canonicalizes_tracking(self) -> None:
        self.assertEqual(set(COLLECTOR_REGISTRY), {"rss"})
        source = SourceConfig("RSS", "https://example.com/rss")
        batch = collect_source(
            source,
            self.network,
            lambda request, timeout: FakeResponse(RSS),
        )
        self.assertIsInstance(batch, CollectionBatch)
        self.assertEqual(len(batch), 2)
        self.assertEqual(
            canonicalize_url(
                "HTTPS://Example.COM:443/one?utm_source=x&b=2&a=1#section"
            ),
            "https://example.com/one?a=1&b=2",
        )
        item = NewsItem(
            "Source", "id", "title", "", "https://example.com/a#one", "", "", "", ""
        )
        self.assertEqual(item.dedupe_key, "https://example.com/a")

    def test_unsupported_collector_fails_fast(self) -> None:
        from scout.config import ConfigError

        with self.assertRaisesRegex(ConfigError, "must be one of"):
            SourceConfig("Source", "https://example.com/rss", collector="llm")


if __name__ == "__main__":
    unittest.main()
