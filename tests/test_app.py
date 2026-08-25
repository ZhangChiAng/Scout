import json
import tempfile
import unittest
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from scout.app import run
from scout.collector import CollectionBatch, CollectionError, CollectionIssue
from scout.config import (
    AppConfig,
    FeishuConfig,
    FeishuDeliveryConfig,
    NetworkConfig,
    SourceConfig,
)
from scout.notifier import NotificationError
from scout.storage import SQLiteStorage
from tests.helpers import news_item

DELIVERY = FeishuDeliveryConfig("cli_test", "app-secret", "chat_id", "oc_private")

ISSUE_HTML = (
    "<h1>AI 早报 2026-08-24</h1>"
    "<h2>概览</h2><h3>要闻</h3>"
    "<ul><li>DeepSeek 发布新模型 <a href='https://example.com/news/1'>↗</a>"
    " <code>#1</code></li></ul>"
)


def issue_item(  # type: ignore[no-any-unimported]
    *,
    item_id: str = "2026-08-24",
    url: str = "https://daily.juya.uk/issues/2026-08-24/",
    content: str = ISSUE_HTML,
    published_at: str = "2026-08-24T09:30:00+08:00",
):
    return news_item(
        source="juya-ai-daily",
        item_id=item_id,
        guid=item_id,
        title=item_id,
        url=url,
        content=content,
        published_at=published_at,
    )


def test_config(max_payload_bytes: int = 28 * 1024) -> AppConfig:
    return AppConfig(
        (SourceConfig("juya-ai-daily", "https://daily.juya.uk/rss.xml"),),
        network=NetworkConfig(15, 5 * 1024 * 1024, "test"),
        feishu=FeishuConfig(max_payload_bytes),
    )


class FixedCollector:
    def __init__(self, source: object, network: object) -> None:
        pass

    def collect(self) -> list[object]:
        return [issue_item()]


class AppTests(unittest.TestCase):
    def run_app(self, **overrides: object) -> int:
        kwargs: dict[str, object] = {
            "mode": "dry-run",
            "database_path": "unused.sqlite3",
            "output": StringIO(),
            "collector_factory": FixedCollector,
            "clock": lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
        }
        kwargs.update(overrides)
        config = kwargs.pop("config", test_config())
        return run(config, **kwargs)  # type: ignore[arg-type]

    def test_send_requires_feishu_delivery_before_collecting(self) -> None:
        class ForbiddenCollector:
            def __init__(self, source: object, network: object) -> None:
                raise AssertionError("collector should not be created")

        with self.assertRaisesRegex(ValueError, "Feishu delivery configuration"):
            self.run_app(mode="send", collector_factory=ForbiddenCollector)

    def test_dry_run_previews_baseline_without_feishu_config_or_database_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "state.sqlite3"
            output = StringIO()

            def forbidden_notifier(*args: object) -> object:
                raise AssertionError("dry-run created a notifier")

            for _ in range(2):
                result = self.run_app(
                    database_path=path,
                    output=output,
                    notifier_factory=forbidden_notifier,
                )
                self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertIn("Baseline preview: juya-ai-daily: 1 item(s)", rendered)
            self.assertIn("2026-08-24", rendered)
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_empty_database_send_establishes_baseline_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            sent: list[object] = []

            class RecordingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)

            result, output = self._send(
                path,
                notifier_factory=RecordingNotifier,
            )
            self.assertEqual(result, 0)
            storage = SQLiteStorage(path)
            self.assertTrue(
                storage.is_source_initialized("juya-ai-daily", read_only=True)
            )
            self.assertEqual(
                storage.baseline_items("juya-ai-daily", read_only=True),
                frozenset({issue_item().dedupe_key}),
            )
            self.assertEqual(sent, [])
            self.assertIn(
                "Baseline created: juya-ai-daily: 1 item(s)", output.getvalue()
            )

    def test_second_run_deduplicates_delivered_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            SQLiteStorage(path).initialize_source_baseline("juya-ai-daily", ())
            sent: list[object] = []

            class RecordingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)

            outputs = []
            for _ in range(2):
                result, output = self._send(path, notifier_factory=RecordingNotifier)
                self.assertEqual(result, 0)
                outputs.append(output.getvalue())

            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0].items, (issue_item(),))
            self.assertIn("sent=1", outputs[0])
            self.assertIn("No new items.", outputs[1])
            self.assertEqual(SQLiteStorage(path).unseen([issue_item()]), [])

    def test_dry_run_previews_payload_with_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.sqlite3"
            SQLiteStorage(path).initialize_source_baseline("juya-ai-daily", ())
            before = path.read_bytes()
            output = StringIO()
            result = self.run_app(
                database_path=path,
                output=output,
            )
            self.assertEqual(result, 0)
            self.assertEqual(path.read_bytes(), before)

        payload = json.loads(output.getvalue().splitlines()[0])
        post = payload["content"]["zh_cn"]
        self.assertEqual(post["title"], "2026-08-24 · Scout · juya-ai-daily")
        self.assertIn("DeepSeek 发布新模型", json.dumps(post, ensure_ascii=False))
        self.assertIn("查看全文", json.dumps(post, ensure_ascii=False))
        self.assertIn("previewed=1", output.getvalue())

    def test_failed_send_is_not_delivered_and_alert_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            SQLiteStorage(path).initialize_source_baseline("juya-ai-daily", ())
            sent: list[object] = []

            class FailingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)
                    if digest.items:  # type: ignore[attr-defined]
                        raise NotificationError("rejected")

            for _ in range(2):
                result, _ = self._send(path, notifier_factory=FailingNotifier)
                self.assertEqual(result, 1)

            content_attempts = [digest for digest in sent if digest.items]  # type: ignore[attr-defined]
            alerts = [digest for digest in sent if not digest.items]  # type: ignore[attr-defined]
            self.assertEqual(len(content_attempts), 2)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(SQLiteStorage(path).unseen([issue_item()]), [issue_item()])
            self.assertIn("失败阶段：send", alerts[0].encoded.decode("utf-8"))

    def test_delivery_recovers_active_failure_and_later_failure_rearms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            SQLiteStorage(path).initialize_source_baseline("juya-ai-daily", ())
            storage = SQLiteStorage(path)
            item = issue_item()
            sent: list[object] = []

            class PhaseNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)
                    if digest.items and phase == 0:  # type: ignore[attr-defined]
                        raise NotificationError("rejected")

            phase = 0
            self.assertEqual(self._send(path, notifier_factory=PhaseNotifier)[0], 1)
            self.assertTrue(
                storage.has_active_failure("juya-ai-daily", item.dedupe_key)
            )

            phase = 1
            result, _ = self._send(path, notifier_factory=PhaseNotifier)
            self.assertEqual(result, 0)
            self.assertFalse(
                storage.has_active_failure("juya-ai-daily", item.dedupe_key)
            )
            self.assertTrue(storage.is_delivered(item, read_only=True))

            phase = 0
            other = issue_item(
                item_id="2026-08-25", url="https://daily.juya.uk/issues/2026-08-25/"
            )

            class OtherCollector:
                def __init__(self, source: object, network: object) -> None:
                    pass

                def collect(self) -> list[object]:
                    return [other]

            self.assertEqual(
                self._send(
                    path,
                    collector_factory=OtherCollector,
                    notifier_factory=PhaseNotifier,
                )[0],
                1,
            )

        alerts = [digest for digest in sent if not digest.items]  # type: ignore[attr-defined]
        self.assertEqual(len(alerts), 2)

    def test_digest_parse_failure_is_isolated_and_alerted_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            SQLiteStorage(path).initialize_source_baseline("juya-ai-daily", ())
            sent: list[object] = []

            class RecordingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)

            class BrokenCollector:
                def __init__(self, source: object, network: object) -> None:
                    pass

                def collect(self) -> list[object]:
                    return [issue_item(content="<h1>无概览</h1>")]

            for _ in range(2):
                result, _ = self._send(
                    path,
                    collector_factory=BrokenCollector,
                    notifier_factory=RecordingNotifier,
                )
                self.assertEqual(result, 1)

        content = [digest for digest in sent if digest.items]  # type: ignore[attr-defined]
        alerts = [digest for digest in sent if not digest.items]  # type: ignore[attr-defined]
        self.assertEqual(content, [])
        self.assertEqual(len(alerts), 1)
        self.assertIn("失败阶段：message", alerts[0].encoded.decode("utf-8"))

    def test_message_oversize_failure_does_not_block_later_issue(self) -> None:
        first = issue_item(
            item_id="2026-08-23", url="https://daily.juya.uk/issues/2026-08-23/"
        )
        oversized_html = (
            "<h1>AI 早报 2026-08-22</h1><h2>概览</h2><h3>要闻</h3><ul>"
            + "".join(
                f"<li>标题 {index} {'长标题' * 60} <a href='https://example.com/{index}'>↗</a></li>"
                for index in range(50)
            )
            + "</ul>"
        )
        second = issue_item(
            item_id="2026-08-22",
            url="https://daily.juya.uk/issues/2026-08-22/",
            content=oversized_html,
        )

        class TwoCollector:
            def __init__(self, source: object, network: object) -> None:
                pass

            def collect(self) -> list[object]:
                return [first, second]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            SQLiteStorage(path).initialize_source_baseline("juya-ai-daily", ())
            storage = SQLiteStorage(path)
            sent: list[object] = []

            class RecordingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)

            result, _ = self._send(
                path,
                collector_factory=TwoCollector,
                notifier_factory=RecordingNotifier,
            )
            self.assertEqual(result, 1)
            self.assertTrue(storage.is_delivered(first, read_only=True))
            self.assertFalse(storage.is_delivered(second, read_only=True))

        content = [digest for digest in sent if digest.items]  # type: ignore[attr-defined]
        alerts = [digest for digest in sent if not digest.items]  # type: ignore[attr-defined]
        self.assertEqual(
            [digest.items[0].item_id for digest in content], ["2026-08-23"]
        )  # type: ignore[attr-defined]
        self.assertEqual(len(alerts), 1)
        self.assertIn("失败阶段：message", alerts[0].encoded.decode("utf-8"))

    def test_source_collection_failure_alerts_once_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            SQLiteStorage(path).initialize_source_baseline("juya-ai-daily", ())
            storage = SQLiteStorage(path)
            sent: list[object] = []
            current: object = CollectionError("offline")

            class RecordingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)

            class OutcomeCollector:
                def __init__(self, source: object, network: object) -> None:
                    pass

                def collect(self) -> CollectionBatch:
                    if isinstance(current, BaseException):
                        raise current
                    return current  # type: ignore[return-value]

            for expected in (1, 1):
                result, _ = self._send(
                    path,
                    collector_factory=OutcomeCollector,
                    notifier_factory=RecordingNotifier,
                )
                self.assertEqual(result, expected)
            self.assertTrue(
                storage.has_active_failure(
                    "juya-ai-daily", "__source__", read_only=True
                )
            )

            current = CollectionBatch(())
            recovered, _ = self._send(
                path,
                collector_factory=OutcomeCollector,
                notifier_factory=RecordingNotifier,
            )
            self.assertEqual(recovered, 0)
            self.assertFalse(
                storage.has_active_failure(
                    "juya-ai-daily", "__source__", read_only=True
                )
            )

            current = CollectionError("offline again")
            failed_again, _ = self._send(
                path,
                collector_factory=OutcomeCollector,
                notifier_factory=RecordingNotifier,
            )
            self.assertEqual(failed_again, 1)

        alerts = [digest for digest in sent if not digest.items]  # type: ignore[attr-defined]
        self.assertEqual(len(alerts), 2)
        self.assertTrue(
            all(
                "失败阶段：collection" in digest.encoded.decode("utf-8")
                for digest in alerts
            )
        )

    def test_collection_issue_defers_baseline_and_alerts(self) -> None:
        issue = CollectionIssue(
            source="juya-ai-daily",
            stage="date parsing",
            title="Broken entry",
            url="https://daily.juya.uk/issues/broken",
            message="invalid date",
            index=1,
        )

        class IssueCollector:
            def __init__(self, source: object, network: object) -> None:
                pass

            def collect(self) -> CollectionBatch:
                return CollectionBatch((), (issue,))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            sent: list[object] = []

            class RecordingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)

            result, output = self._send(
                path,
                collector_factory=IssueCollector,
                notifier_factory=RecordingNotifier,
            )
            self.assertEqual(result, 1)
            storage = SQLiteStorage(path)
            self.assertFalse(
                storage.is_source_initialized("juya-ai-daily", read_only=True)
            )
            self.assertIn(
                "Baseline deferred: juya-ai-daily: 1 collection issue(s)",
                output.getvalue(),
            )

        alerts = [digest for digest in sent if not digest.items]  # type: ignore[attr-defined]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Broken entry", alerts[0].encoded.decode("utf-8"))

    def test_age_gate_skips_stale_issue_and_alerts_once(self) -> None:
        stale = issue_item(
            item_id="2026-07-01",
            url="https://daily.juya.uk/issues/2026-07-01/",
            published_at="2026-07-01T09:30:00+08:00",
        )
        fresh = issue_item()

        class TwoCollector:
            def __init__(self, source: object, network: object) -> None:
                pass

            def collect(self) -> list[object]:
                return [stale, fresh]

        config = AppConfig(
            (
                SourceConfig(
                    "juya-ai-daily", "https://daily.juya.uk/rss.xml", max_age_days=30
                ),
            ),
            network=NetworkConfig(15, 5 * 1024 * 1024, "test"),
            feishu=FeishuConfig(28 * 1024),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            SQLiteStorage(path).initialize_source_baseline("juya-ai-daily", ())
            sent: list[object] = []

            class RecordingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)

            result, output = self._send(
                path,
                config=config,
                collector_factory=TwoCollector,
                notifier_factory=RecordingNotifier,
            )
            self.assertEqual(result, 1)
            self.assertIn("skipped=1", output.getvalue())

        content = [digest for digest in sent if digest.items]  # type: ignore[attr-defined]
        alerts = [digest for digest in sent if not digest.items]  # type: ignore[attr-defined]
        self.assertEqual(
            [digest.items[0].item_id for digest in content], ["2026-08-24"]
        )  # type: ignore[attr-defined]
        self.assertEqual(len(alerts), 1)
        self.assertIn("失败阶段：age", alerts[0].encoded.decode("utf-8"))

    def _send(
        self,
        path: Path,
        **overrides: object,
    ) -> tuple[int, StringIO]:
        output = StringIO()
        kwargs: dict[str, object] = {
            "mode": "send",
            "database_path": path,
            "output": output,
            "feishu_delivery": DELIVERY,
            "collector_factory": FixedCollector,
        }
        kwargs.update(overrides)
        config = kwargs.pop("config", test_config())
        result = run(config, **kwargs)  # type: ignore[arg-type]
        return result, output


if __name__ == "__main__":
    unittest.main()
