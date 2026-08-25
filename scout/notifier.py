"""Feishu application-bot rich-text notification support."""

import atexit
import json
from collections.abc import Callable
from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from lark_oapi.ws.client import loop as _lark_ws_loop

from .config import FeishuDeliveryConfig
from .digest import IssueDigest
from .model import NewsItem


def _close_unused_lark_ws_loop() -> None:
    """Close the SDK's import-time websocket loop, unused by this HTTP client."""

    if not _lark_ws_loop.is_closed() and not _lark_ws_loop.is_running():
        _lark_ws_loop.close()


atexit.register(_close_unused_lark_ws_loop)


class NotificationError(RuntimeError):
    """Raised when Feishu does not accept a notification."""


@dataclass(frozen=True, slots=True)
class Digest:
    payload: dict[str, object]
    items: tuple[NewsItem, ...]
    encoded: bytes


def build_failure_digest(
    *,
    title: str,
    source: str,
    article_title: str,
    stage: str,
    max_payload_bytes: int,
) -> Digest:
    """Build the small, non-recursive alert sent after an item failure."""

    paragraphs = [
        [
            {
                "tag": "text",
                "text": (
                    f"来源：{source[:200]}\n"
                    f"文章：{article_title[:500]}\n"
                    f"失败阶段：{stage[:100]}\n"
                ),
            }
        ]
    ]
    payload = _payload(title, paragraphs)
    encoded = encode_payload(payload)
    if len(encoded) > max_payload_bytes:
        raise NotificationError("failure alert exceeds the configured payload limit")
    return Digest(payload=payload, items=(), encoded=encoded)


def build_issue_digest(
    issue: IssueDigest,
    *,
    items: tuple[NewsItem, ...] = (),
    title: str,
    max_payload_bytes: int,
) -> Digest:
    """Build the single Feishu message for one daily issue.

    The overview renders as one section per category, each headline a blue
    link to its source, followed by a full-page link.  The curated overview
    is a few kilobytes, but the payload limit is still enforced.
    """

    paragraphs: list[list[dict[str, object]]] = []
    category_order: list[str] = []
    for entry in issue.overview:
        if entry.category not in category_order:
            category_order.append(entry.category)
            paragraphs.append([{"tag": "text", "text": f"【{entry.category}】"}])
        paragraphs.append(
            [_headline_node(entry.headline, entry.url)],
        )
    if not paragraphs:
        raise NotificationError("issue overview contains no entries")
    page_url = issue.page_url
    if page_url:
        paragraphs.append([_headline_node("查看全文", page_url)])
    payload = _payload(title, paragraphs)
    encoded = encode_payload(payload)
    if len(encoded) > max_payload_bytes:
        raise NotificationError(
            f"issue digest cannot fit within {max_payload_bytes} byte payload limit"
        )
    return Digest(payload=payload, items=items, encoded=encoded)


def encode_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _payload(
    title: str, paragraphs: list[list[dict[str, object]]]
) -> dict[str, object]:
    return {
        "msg_type": "post",
        "content": {"zh_cn": {"title": title, "content": paragraphs}},
    }


def _headline_node(text: str, url: str) -> dict[str, object]:
    if url:
        return {"tag": "a", "text": f"{text}\n", "href": url}
    return {"tag": "text", "text": f"{text}\n"}


class FeishuNotifier:
    def __init__(
        self,
        delivery: FeishuDeliveryConfig,
        timeout_seconds: float,
        client_factory: Callable[[FeishuDeliveryConfig, float], object] | None = None,
    ) -> None:
        self.delivery = delivery
        factory = _build_client if client_factory is None else client_factory
        try:
            self._client = factory(delivery, timeout_seconds)
        except Exception as exc:
            raise NotificationError(
                f"Feishu OpenAPI client initialization failed: {type(exc).__name__}"
            ) from exc

    def send(self, digest: Digest) -> None:
        try:
            content = json.dumps(
                digest.payload["content"], ensure_ascii=False, separators=(",", ":")
            )
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(self.delivery.receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(self.delivery.receive_id)
                    .msg_type("post")
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)  # type: ignore[attr-defined]
            success = response.success()
        except Exception as exc:
            raise NotificationError(
                f"Feishu OpenAPI request failed: {type(exc).__name__}"
            ) from exc

        if not success:
            code = getattr(response, "code", None)
            if not isinstance(code, int):
                code = None
            raise NotificationError(f"Feishu rejected the message (code={code!r})")


def _build_client(delivery: FeishuDeliveryConfig, timeout_seconds: float) -> object:
    return (
        lark.Client.builder()
        .app_id(delivery.app_id)
        .app_secret(delivery.app_secret)
        .timeout(timeout_seconds)
        .build()
    )
