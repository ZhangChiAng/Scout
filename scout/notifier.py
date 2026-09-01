"""Feishu interactive cards for personalized articles and feedback."""

from __future__ import annotations

import atexit
import json
from collections.abc import Callable
from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from lark_oapi.ws.client import loop as _lark_ws_loop

from .config import FeishuDeliveryConfig
from .model import DigestArticle, PersonalizedEvaluation, PreferenceProfile
from .storage import CardDelivery, FeedbackEvidence


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
                f"**已记录反馈**：{label}\n"
                f"**原因**：{_md_escape(feedback.reason)}\n"
                f"**反馈修订 ID**：{feedback.revision_id}"
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
            _markdown(rules("喜欢规则", profile.like_rules)),
            _markdown(rules("不喜欢规则", profile.dislike_rules)),
            _markdown(rules("权衡项", profile.tradeoffs)),
            _markdown(rules("不确定项", profile.uncertainties)),
            {"tag": "hr"},
            _markdown(f"**版本变化**\n{_md_escape(profile.change_summary)}"),
        ],
    }
    _check_card_size(card, max_payload_bytes)
    return card


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

    def send_card(self, card: dict[str, object]) -> SentMessage:
        try:
            content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(self.delivery.receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(self.delivery.receive_id)
                    .msg_type("interactive")
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)  # type: ignore[attr-defined]
        except Exception as exc:
            raise NotificationError(
                f"Feishu OpenAPI request failed: {type(exc).__name__}"
            ) from exc
        if not response.success():
            code = getattr(response, "code", None)
            raise NotificationError(f"Feishu rejected the card (code={code!r})")
        data = getattr(response, "data", None)
        message_id = getattr(data, "message_id", None)
        chat_id = getattr(data, "chat_id", None)
        if not isinstance(message_id, str) or not message_id:
            raise NotificationError("Feishu success response has no message_id")
        if not isinstance(chat_id, str) or not chat_id:
            chat_id = self.delivery.receive_id
        return SentMessage(message_id=message_id, chat_id=chat_id)


def _build_client(delivery: FeishuDeliveryConfig, timeout_seconds: float) -> object:
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
