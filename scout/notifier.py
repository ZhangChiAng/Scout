"""Feishu interactive cards for personalized articles and feedback."""

from __future__ import annotations

import atexit
import json
from collections.abc import Sequence
from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)
from lark_oapi.ws.client import loop as _lark_ws_loop

from .config import FeishuDeliveryConfig
from .issue_state import FilteredList, ListMember
from .model import (
    CardDelivery,
    DigestArticle,
    FeedbackEvidence,
    PersonalizedEvaluation,
    PreferenceProfile,
)


def _close_unused_lark_ws_loop() -> None:
    if not _lark_ws_loop.is_closed() and not _lark_ws_loop.is_running():
        _lark_ws_loop.close()


atexit.register(_close_unused_lark_ws_loop)


class NotificationError(RuntimeError):
    """Raised when a card is invalid or Feishu rejects it."""


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: str
    chat_id: str


def build_article_card(
    article: DigestArticle,
    evaluation: PersonalizedEvaluation,
    *,
    snapshot_id: int,
    purpose: str,
    max_payload_bytes: int,
    feedback: FeedbackEvidence | None = None,
) -> dict[str, object]:
    reference = {"snapshot_id": snapshot_id, "purpose": purpose}
    elements = _article_elements(article, evaluation)
    if feedback is not None:
        label = "喜欢" if feedback.sentiment == "like" else "不喜欢"
        elements.extend(
            [
                {"tag": "hr"},
                _markdown(
                    f"**已记录反馈**：{label}\n**原因**：{_md_escape(feedback.reason)}"
                ),
            ]
        )
    elements.append(
        {
            "tag": "action",
            "actions": [
                _button("喜欢", "primary", reference, "like"),
                _button("不喜欢", "danger", reference, "dislike"),
            ],
        }
    )
    return _article_card(
        article,
        evaluation,
        elements=elements,
        max_payload_bytes=max_payload_bytes,
    )


def _article_elements(
    article: DigestArticle, evaluation: PersonalizedEvaluation
) -> list[dict[str, object]]:
    link_title = _md_escape(article.title)
    title_line = (
        f"[{link_title}]({_link_url(article.article_url)})"
        if article.article_url
        else link_title
    )
    number = f" · #{article.number}" if article.number else ""
    return [
        _markdown(f"**{title_line}**"),
        _markdown(f"**分类**：{_md_escape(article.category or '未分类')}{number}"),
        {"tag": "hr"},
        _markdown(f"**橘鸦摘要**\n{_md_escape(article.summary or '（未提供摘要）')}"),
        _markdown(f"**详情**\n{_md_escape(article.detail or '（未提供详情）')}"),
        {"tag": "hr"},
        _markdown(
            f"**Scout 判断：{evaluation.verdict}**\n{_md_escape(evaluation.reason)}"
        ),
    ]


def _article_card(
    article: DigestArticle,
    evaluation: PersonalizedEvaluation,
    *,
    elements: list[dict[str, object]],
    max_payload_bytes: int,
) -> dict[str, object]:
    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": _verdict_template(evaluation.verdict),
            "title": {
                "tag": "plain_text",
                "content": f"{evaluation.verdict} · {article.title}"[:120],
            },
        },
        "elements": elements,
    }
    _check_card_size(card, max_payload_bytes)
    return card


def build_feedback_form_card(
    delivery: CardDelivery,
    *,
    sentiment: str,
    max_payload_bytes: int,
    error: str = "",
) -> dict[str, object]:
    label = "喜欢" if sentiment == "like" else "不喜欢"
    reference = {
        "action": "submit",
        "snapshot_id": delivery.snapshot_id,
        "purpose": delivery.purpose,
        "sentiment": sentiment,
    }
    error_elements = (
        [_markdown(f"<font color='red'>{_md_escape(error)}</font>")] if error else []
    )
    evaluation = PersonalizedEvaluation(
        delivery.article.article_key, delivery.verdict, delivery.reason
    )
    elements = _article_elements(delivery.article, evaluation)
    elements.extend(
        [
            {"tag": "hr"},
            _markdown(f"已选择 **{label}**。请填写原因后提交（必填，1–500 字）。"),
            *error_elements,
            {
                "tag": "form",
                "name": "scout_feedback",
                "elements": [
                    {
                        "tag": "input",
                        "name": "reason",
                        "required": True,
                        "max_length": 500,
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "请说明为什么喜欢或不喜欢这条新闻",
                        },
                        "label": {"tag": "plain_text", "content": "原因"},
                        "label_position": "top",
                    },
                    {
                        "tag": "button",
                        "name": "submit_feedback",
                        "action_type": "form_submit",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "提交反馈"},
                        "value": reference,
                    },
                ],
            },
        ]
    )
    return _article_card(
        delivery.article,
        evaluation,
        elements=elements,
        max_payload_bytes=max_payload_bytes,
    )


def build_recorded_card(
    delivery: CardDelivery,
    feedback: FeedbackEvidence,
    *,
    max_payload_bytes: int,
) -> dict[str, object]:
    label = "喜欢" if feedback.sentiment == "like" else "不喜欢"
    reference = {
        "action": "edit",
        "snapshot_id": delivery.snapshot_id,
        "purpose": delivery.purpose,
    }
    evaluation = PersonalizedEvaluation(
        delivery.article.article_key, delivery.verdict, delivery.reason
    )
    elements = _article_elements(delivery.article, evaluation)
    elements.extend(
        [
            {"tag": "hr"},
            _markdown(
                f"**已记录反馈**：{label}\n**原因**：{_md_escape(feedback.reason)}"
            ),
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "default",
                        "text": {"tag": "plain_text", "content": "修改反馈"},
                        "value": reference,
                    }
                ],
            },
        ]
    )
    return _article_card(
        delivery.article,
        evaluation,
        elements=elements,
        max_payload_bytes=max_payload_bytes,
    )


def build_profile_card(
    profile: PreferenceProfile, *, max_payload_bytes: int
) -> dict[str, object]:
    def rules(title: str, values: tuple[str, ...]) -> str:
        body = "\n".join(f"- {_md_escape(value)}" for value in values) or "- 暂无"
        return f"**{title}**\n{body}"

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "purple",
            "title": {
                "tag": "plain_text",
                "content": f"Scout 偏好档案 v{profile.version} 已生效",
            },
        },
        "elements": [
            *[
                _markdown(rules(label, values))
                for label, values in profile.readable_sections()
            ],
            {"tag": "hr"},
            _markdown(f"**版本变化**\n{_md_escape(profile.change_summary)}"),
        ],
    }
    _check_card_size(card, max_payload_bytes)
    return card


def build_filtered_list_card(
    listing: FilteredList, *, max_payload_bytes: int
) -> dict[str, object]:
    checkers = []
    for member in listing.members:
        locked = member.status in {"pending", "sending", "delivered"}
        checkers.append(
            {
                "tag": "checker",
                "name": f"member_{member.member_id}",
                "text": {"tag": "plain_text", "content": member.article.title},
                "checked": locked,
                "disabled": locked,
                "overall_checkable": True,
                "checked_style": {"show_strikethrough": False, "opacity": 1},
                "disabled_tips": {
                    "tag": "plain_text",
                    "content": "正文已送达"
                    if member.status == "delivered"
                    else "正文正在发送",
                },
            }
        )
    delivered = sum(m.status == "delivered" for m in listing.members)
    pending = sum(m.status in {"pending", "sending"} for m in listing.members)
    failed = sum(m.status == "failed" for m in listing.members)
    note = f"已送达 {delivered} 条 · 发送中 {pending} 条。可继续勾选其他新闻。"
    if failed:
        note += f" {failed} 条发送失败，可重新勾选重试。"
    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "grey",
            "title": {
                "tag": "plain_text",
                "content": (
                    f"{listing.issue_date} · 不推荐 · {len(listing.members)} 条"
                    + (
                        f" · 第 {listing.page}/{listing.pages} 张"
                        if listing.pages > 1
                        else ""
                    )
                ),
            },
        },
        "elements": [
            _markdown(note),
            {
                "tag": "form",
                "name": "scout_reveal",
                "elements": [
                    *checkers,
                    {
                        "tag": "button",
                        "name": "reveal_selected",
                        "action_type": "form_submit",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "推送所选正文"},
                        "value": {
                            "action": "reveal_selected",
                            "list_id": listing.list_id,
                        },
                    },
                ],
            },
        ],
    }
    _check_card_size(card, max_payload_bytes)
    # Budget the encoded request too: nested JSON escapes quotes and slashes.
    _check_card_size(
        {
            "receive_id": listing.chat_id or "x" * 64,
            "msg_type": "interactive",
            "uuid": listing.send_uuid or "x" * 36,
            "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
        },
        max_payload_bytes,
    )
    return card


def partition_filtered_members(
    members: Sequence[ListMember], *, issue_date: str, max_payload_bytes: int
) -> tuple[tuple[ListMember, ...], ...]:
    """Split on both component count and encoded capacity; never truncate titles."""
    pages: list[tuple[ListMember, ...]] = []
    current: list[ListMember] = []
    for member in members:
        candidate = [*current, member]
        try:
            # Budget for real SQLite IDs, page numbers and future status text.
            preview = FilteredList(
                2**63 - 1,
                "",
                issue_date,
                999999,
                999999,
                "",
                None,
                "",
                0,
                tuple(candidate),
            )
            build_filtered_list_card(
                preview, max_payload_bytes=max_payload_bytes - 2048
            )
            if len(candidate) > 50:
                raise NotificationError("list component limit reached")
        except NotificationError:
            if not current:
                raise
            pages.append(tuple(current))
            current = [member]
            preview = FilteredList(
                2**63 - 1, "", issue_date, 999999, 999999, "", None, "", 0, (member,)
            )
            build_filtered_list_card(
                preview, max_payload_bytes=max_payload_bytes - 2048
            )
        else:
            current = candidate
    if current:
        pages.append(tuple(current))
    return tuple(pages)


class FeishuNotifier:
    def __init__(
        self,
        delivery: FeishuDeliveryConfig,
        timeout_seconds: float,
    ) -> None:
        self.delivery = delivery
        try:
            self._client = _build_client(delivery, timeout_seconds)
        except Exception as exc:
            raise NotificationError(
                f"Feishu OpenAPI client initialization failed: {type(exc).__name__}"
            ) from exc

    def send_card(
        self,
        card: dict[str, object],
        *,
        send_uuid: str | None = None,
        chat_id: str | None = None,
    ) -> SentMessage:
        return self._send("interactive", card, send_uuid=send_uuid, chat_id=chat_id)

    def send_link(
        self, title: str, url: str, *, send_uuid: str, chat_id: str
    ) -> SentMessage:
        return self._send(
            "text", {"text": f"{title}\n{url}"}, send_uuid=send_uuid, chat_id=chat_id
        )

    def _send(
        self,
        message_type: str,
        payload: dict,
        *,
        send_uuid: str | None,
        chat_id: str | None,
    ) -> SentMessage:
        try:
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(
                    "chat_id" if chat_id else self.delivery.receive_id_type
                )
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id or self.delivery.receive_id)
                    .msg_type(message_type)
                    .content(content)
                    .uuid(send_uuid)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
        except Exception as exc:
            raise NotificationError(
                f"Feishu OpenAPI request failed: {type(exc).__name__}"
            ) from exc
        if not response.success():
            code = getattr(response, "code", None)
            raise NotificationError(f"Feishu rejected the message (code={code!r})")
        data = getattr(response, "data", None)
        message_id = getattr(data, "message_id", None)
        response_chat_id = getattr(data, "chat_id", None)
        if not isinstance(message_id, str) or not message_id:
            raise NotificationError("Feishu success response has no message_id")
        if not isinstance(response_chat_id, str) or not response_chat_id:
            response_chat_id = chat_id or self.delivery.receive_id
        return SentMessage(message_id=message_id, chat_id=response_chat_id)

    def update_card(self, message_id: str, card: dict[str, object]) -> None:
        try:
            request = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(
                        json.dumps(card, ensure_ascii=False, separators=(",", ":"))
                    )
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.patch(request)
        except Exception as exc:
            raise NotificationError(
                f"Feishu card update failed: {type(exc).__name__}"
            ) from exc
        if not response.success():
            raise NotificationError(
                f"Feishu rejected card update (code={response.code!r})"
            )


def _build_client(
    delivery: FeishuDeliveryConfig, timeout_seconds: float
) -> lark.Client:
    return (
        lark.Client.builder()
        .app_id(delivery.app_id)
        .app_secret(delivery.app_secret)
        .timeout(timeout_seconds)
        .build()
    )


def _button(
    text: str,
    button_type: str,
    reference: dict[str, object],
    sentiment: str,
) -> dict[str, object]:
    return {
        "tag": "button",
        "type": button_type,
        "text": {"tag": "plain_text", "content": text},
        "value": {"action": "select", "sentiment": sentiment, **reference},
    }


def _markdown(content: str) -> dict[str, object]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _md_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("*", "\\*")
    )


def _link_url(value: str) -> str:
    return value.replace(")", "%29").replace("(", "%28")


def _verdict_template(verdict: str) -> str:
    return {"推荐": "green", "不推荐": "red", "不确定": "orange"}[verdict]


def _check_card_size(card: dict[str, object], max_payload_bytes: int) -> None:
    encoded = json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > max_payload_bytes:
        raise NotificationError(
            f"interactive card exceeds {max_payload_bytes} byte payload limit"
        )
