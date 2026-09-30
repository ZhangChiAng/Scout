"""Start durable Zhihu scans from the sole owner's group mentions."""

from __future__ import annotations

import json
import logging
import threading

from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.core.model import BaseRequest

from . import zhihu_store
from .database import transaction
from .notifier import NotificationError, _build_client
from .zhihu_cards import _card
from .zhihu_workflow import _cards

logger = logging.getLogger(__name__)


def bot_open_id(delivery, timeout_seconds):
    request = BaseRequest()
    request.http_method = HttpMethod.GET
    request.uri = "/open-apis/bot/v3/info"
    request.token_types = {AccessTokenType.TENANT}
    response = _build_client(delivery, timeout_seconds).request(request)
    if not response.success():
        raise NotificationError(
            f"Cannot resolve Feishu bot identity (code={response.code})"
        )
    payload = json.loads(response.raw.content)
    identity = (payload.get("bot") or {}).get("open_id")
    if not isinstance(identity, str) or not identity:
        raise NotificationError("Feishu bot identity response has no open_id")
    return identity


class ZhihuMessageHandler:
    def __init__(self, database, *, chat_id, bot_id, max_payload_bytes, wake):
        self.database = database
        self.chat_id = chat_id
        self.bot_id = bot_id
        self.max_payload_bytes = max_payload_bytes
        self.wake = wake

    def __call__(self, callback):
        event = callback.event
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if (
            message is None
            or message.chat_type != "group"
            or message.chat_id != self.chat_id
            or not message.message_id
            or getattr(sender, "sender_type", None) != "user"
        ):
            return
        mentions = message.mentions or []
        bot_keys = [
            m.key
            for m in mentions
            if getattr(m.id, "open_id", None) == self.bot_id and m.key
        ]
        if not bot_keys:
            return
        open_id = getattr(getattr(sender, "sender_id", None), "open_id", None)
        if not open_id:
            return
        description = ""
        if message.message_type == "text":
            try:
                content = json.loads(message.content)
                description = (
                    content.get("text", "") if isinstance(content, dict) else ""
                )
            except ValueError, TypeError:
                description = ""
            if not isinstance(description, str):
                description = ""
            for key in bot_keys:
                description = description.replace(key, "")
            # Preserve other mentioned names instead of platform placeholders.
            for mention in mentions:
                if mention.key and mention.key not in bot_keys:
                    description = description.replace(mention.key, mention.name or "")
            description = description.strip()
        valid = 1 <= len(description) <= 5000
        event_id = "message:" + message.message_id
        with transaction(self.database, rows=True, timeout=0.3) as conn:
            owner = conn.execute(
                "SELECT open_id FROM scout_owner WHERE singleton=1"
            ).fetchone()
            if owner is not None and owner[0] != open_id:
                return
            if valid:
                zhihu_store.authorize_owner(self.database, open_id, connection=conn)
                topic = zhihu_store.save_topic(
                    self.database,
                    description[:100],
                    description=description,
                    event_id=event_id,
                    connection=conn,
                )
                zhihu_store.enqueue_job(
                    self.database,
                    "scan",
                    {"topic_id": topic["id"]},
                    event_id=event_id,
                    connection=conn,
                )
                text = (
                    f"已安排知乎搜索：{topic['name']}\n"
                    "范围为近 30 天，逐条推送，成功送达 5 条后暂停。可通过卡片继续、停止或反馈，继续无需先反馈。"
                )
            else:
                text = "请 @ 我并用 1–5000 字的文字描述感兴趣的主题，例如：@Scout 我想看 AI 编程工具的实际使用经验。"
            card = _card(
                "Scout · 知乎搜索",
                [
                    {"tag": "div", "text": {"tag": "plain_text", "content": text}},
                ],
                self.max_payload_bytes,
            )
            zhihu_store.enqueue_card(
                self.database,
                card,
                self.chat_id,
                event_id=event_id,
                connection=conn,
            )
        self.wake()


class ZhihuMessageDeliveryWorker:
    """Deliver receipts even while the scan worker is waiting for a model."""

    def __init__(self, database):
        self.database = database
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="scout-zhihu-receipts", daemon=True
        )

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join()

    def _run(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                _cards(self.database, messages_only=True)
            except Exception as exc:  # noqa: BLE001 - durable outbox retries on next tick
                logger.warning("Zhihu receipt delivery failed: %s", type(exc).__name__)
            self.wake.wait(1)
