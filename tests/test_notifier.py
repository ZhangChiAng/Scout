import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scout.config import FeishuDeliveryConfig
from scout.digest import IssueDigest, OverviewEntry, parse_issue
from scout.notifier import (
    FeishuNotifier,
    NotificationError,
    _build_client,
    build_failure_digest,
    build_issue_digest,
)
from tests.helpers import news_item

DELIVERY = FeishuDeliveryConfig(
    app_id="cli_test",
    app_secret="super-secret",
    receive_id_type="chat_id",
    receive_id="oc_private",
)

ISSUE = IssueDigest(
    issue_date="2026-08-24",
    overview=(
        OverviewEntry("要闻", "DeepSeek 发布新模型", "https://example.com/news/1", "1"),
        OverviewEntry("要闻", "OpenAI 更新 API", "https://example.com/news/2", "2"),
        OverviewEntry("开发生态", "某个框架发布新版", "https://example.com/news/3", ""),
        OverviewEntry("开发生态", "没有链接的标题", "", ""),
    ),
    sections=(),
    page_url="https://daily.juya.uk/issues/2026-08-24/",
)


class NotifierTests(unittest.TestCase):
    def test_issue_digest_renders_category_sections_and_full_page_link(self) -> None:
        item = news_item()
        digest = build_issue_digest(
            ISSUE,
            items=(item,),
            title="2026-08-24 · Scout · juya-ai-daily",
            max_payload_bytes=5000,
        )

        post = digest.payload["content"]["zh_cn"]  # type: ignore[index]
        self.assertEqual(post["title"], "2026-08-24 · Scout · juya-ai-daily")
        self.assertEqual(digest.items, (item,))
        self.assertEqual(digest.payload["msg_type"], "post")
        content = post["content"]
        self.assertEqual(content[0], [{"tag": "text", "text": "【要闻】"}])
        self.assertEqual(
            content[1],
            [
                {
                    "tag": "a",
                    "text": "DeepSeek 发布新模型\n",
                    "href": "https://example.com/news/1",
                }
            ],
        )
        self.assertEqual(
            content[2],
            [
                {
                    "tag": "a",
                    "text": "OpenAI 更新 API\n",
                    "href": "https://example.com/news/2",
                }
            ],
        )
        self.assertEqual(content[3], [{"tag": "text", "text": "【开发生态】"}])
        self.assertEqual(
            content[4],
            [
                {
                    "tag": "a",
                    "text": "某个框架发布新版\n",
                    "href": "https://example.com/news/3",
                }
            ],
        )
        self.assertEqual(content[5], [{"tag": "text", "text": "没有链接的标题\n"}])
        self.assertEqual(
            content[6],
            [
                {
                    "tag": "a",
                    "text": "查看全文\n",
                    "href": "https://daily.juya.uk/issues/2026-08-24/",
                }
            ],
        )
        self.assertLessEqual(len(digest.encoded), 5000)

    def test_issue_digest_without_page_url_skips_full_page_line(self) -> None:
        issue = IssueDigest(
            issue_date="2026-08-24",
            overview=ISSUE.overview,
            sections=(),
            page_url="",
        )
        digest = build_issue_digest(
            issue,
            title="2026-08-24 · Scout · juya-ai-daily",
            max_payload_bytes=5000,
        )
        post = digest.payload["content"]["zh_cn"]  # type: ignore[index]
        self.assertEqual(len(post["content"]), 6)
        self.assertNotIn("查看全文", digest.encoded.decode("utf-8"))

    def test_empty_overview_is_rejected(self) -> None:
        issue = IssueDigest(issue_date="", overview=(), sections=(), page_url="")
        with self.assertRaisesRegex(NotificationError, "no entries"):
            build_issue_digest(
                issue,
                title="2026-08-24",
                max_payload_bytes=5000,
            )

    def test_oversized_issue_digest_is_rejected(self) -> None:
        entries = tuple(
            OverviewEntry("要闻", f"标题 {index} " + "长标题" * 500, "", "")
            for index in range(40)
        )
        issue = IssueDigest(
            issue_date="2026-08-24",
            overview=entries,
            sections=(),
            page_url="",
        )
        with self.assertRaisesRegex(NotificationError, "payload limit"):
            build_issue_digest(
                issue,
                title="2026-08-24",
                max_payload_bytes=1000,
            )

    def test_parsed_issue_round_trips_into_a_message(self) -> None:
        html = (
            "<h1>AI 早报 2026-08-24</h1><h2>概览</h2><h3>要闻</h3>"
            "<ul><li>某标题 <a href='https://example.com/a'>↗</a></li></ul>"
        )
        issue = parse_issue(html, page_url="https://daily.juya.uk/issues/2026-08-24/")
        digest = build_issue_digest(
            issue,
            title="2026-08-24 · Scout · juya-ai-daily",
            max_payload_bytes=5000,
        )
        body = digest.encoded.decode("utf-8")
        self.assertIn("某标题", body)
        self.assertIn("查看全文", body)

    def test_failure_digest_contains_source_article_and_stage(self) -> None:
        digest = build_failure_digest(
            title="2026-08-24 · Scout · 告警",
            source="juya-ai-daily",
            article_title="2026-08-24",
            stage="digest",
            max_payload_bytes=5000,
        )

        body = digest.encoded.decode("utf-8")
        self.assertEqual(digest.items, ())
        self.assertIn("来源：juya-ai-daily", body)
        self.assertIn("文章：2026-08-24", body)
        self.assertIn("失败阶段：digest", body)
        self.assertLessEqual(len(digest.encoded), 5000)

    def test_openapi_request_contains_target_post_and_string_content(self) -> None:
        digest = build_issue_digest(
            ISSUE,
            items=(news_item(),),
            title="2026-08-24",
            max_payload_bytes=5000,
        )
        seen: dict[str, object] = {}

        class MessageService:
            def create(self, request: object) -> object:
                seen["request"] = request
                return SimpleNamespace(code=0, success=lambda: True)

        client = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=MessageService()))
        )

        def client_factory(delivery: FeishuDeliveryConfig, timeout: float) -> object:
            seen["delivery"] = delivery
            seen["timeout"] = timeout
            return client

        FeishuNotifier(DELIVERY, 7.0, client_factory).send(digest)
        request = seen["request"]
        self.assertEqual(request.receive_id_type, "chat_id")  # type: ignore[union-attr]
        body = request.request_body  # type: ignore[union-attr]
        self.assertEqual(body.receive_id, "oc_private")
        self.assertEqual(body.msg_type, "post")
        self.assertIsInstance(body.content, str)
        self.assertEqual(json.loads(body.content), digest.payload["content"])
        self.assertEqual(seen["delivery"], DELIVERY)
        self.assertEqual(seen["timeout"], 7.0)

    def test_sdk_client_uses_app_credentials_and_network_timeout(self) -> None:
        calls: list[tuple[str, object]] = []
        built_client = object()

        class Builder:
            def app_id(self, value: str) -> Builder:
                calls.append(("app_id", value))
                return self

            def app_secret(self, value: str) -> Builder:
                calls.append(("app_secret", value))
                return self

            def timeout(self, value: float) -> Builder:
                calls.append(("timeout", value))
                return self

            def build(self) -> object:
                return built_client

        class Client:
            @staticmethod
            def builder() -> Builder:
                return Builder()

        with patch("scout.notifier.lark.Client", Client):
            result = _build_client(DELIVERY, 9.5)

        self.assertIs(result, built_client)
        self.assertEqual(
            calls,
            [
                ("app_id", "cli_test"),
                ("app_secret", "super-secret"),
                ("timeout", 9.5),
            ],
        )

    def test_rejects_business_error_and_sdk_exception_without_secrets(self) -> None:
        digest = build_issue_digest(
            ISSUE,
            title="2026-08-24",
            max_payload_bytes=5000,
        )

        class MessageService:
            def __init__(self, response: object = None, error: Exception | None = None):
                self.response = response
                self.error = error

            def create(self, request: object) -> object:
                if self.error is not None:
                    raise self.error
                return self.response

        cases = (
            MessageService(SimpleNamespace(code=19001, success=lambda: False)),
            MessageService(error=RuntimeError("super-secret oc_private")),
        )
        for service in cases:
            client = SimpleNamespace(
                im=SimpleNamespace(v1=SimpleNamespace(message=service))
            )
            with (
                self.subTest(service=service),
                self.assertRaises(NotificationError) as raised,
            ):
                FeishuNotifier(
                    DELIVERY,
                    5,
                    lambda delivery, timeout, current_client=client: current_client,
                ).send(digest)
            self.assertNotIn("super-secret", str(raised.exception))
            self.assertNotIn("oc_private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
