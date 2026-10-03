"""Start durable Zhihu scans from group mentions."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote

from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.core.model import BaseRequest

from . import zhihu_store
from .config import resolve_feishu_delivery
from .database import transaction
from .notifier import NotificationError, _build_client

logger = logging.getLogger(__name__)


class ReceiptDeliveryError(NotificationError):
    """Keep only response status codes, without message content or credentials."""

    def __init__(self, response):
        super().__init__(
            f"Feishu receipt failed (http_status={response.raw.status_code}, "
            f"code={response.code})"
        )


def _receipt_error(exc):
    return str(exc) if isinstance(exc, ReceiptDeliveryError) else type(exc).__name__


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
    def __init__(self, database, *, chat_id, bot_id, wake):
        self.database = database
        self.chat_id = chat_id
        self.bot_id = bot_id
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
        if message.message_type != "text":
            reason = "暂不支持这种消息格式，请 @ 我并用文字描述话题。"
        elif not description:
            reason = "没有收到话题内容，请在 @ 我后写下感兴趣的话题。"
        elif len(description) > 5000:
            reason = "话题描述超过 5000 字，请缩短后重新发送。"
        else:
            reason = ""
        event_id = "message:" + message.message_id
        try:
            with transaction(self.database, rows=True, timeout=0.3) as conn:
                if conn.execute(
                    "SELECT 1 FROM zhihu_message_receipts WHERE message_id=?",
                    (message.message_id,),
                ).fetchone():
                    return
                # Do not acknowledge a historical event again after upgrading.
                if conn.execute(
                    "SELECT 1 FROM zhihu_card_deliveries WHERE event_id=?",
                    (event_id,),
                ).fetchone():
                    return
                if not reason:
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
                conn.execute(
                    "INSERT INTO zhihu_message_receipts(message_id,kind,reason) VALUES (?,?,?)",
                    (message.message_id, "error" if reason else "received", reason),
                )
        except sqlite3.Error:
            logger.warning("Zhihu request could not be persisted")
            # No task was committed. A DB outage cannot use the durable outbox.
            try:
                send_receipt(
                    message.message_id,
                    "error",
                    "暂时无法保存搜索任务，请稍后重新发送。",
                )
            except Exception as exc:  # noqa: BLE001 - best-effort reply during DB outage
                logger.warning("Zhihu failure reply failed: %s", _receipt_error(exc))
            return
        self.wake()


def send_receipt(message_id, kind, reason=""):
    """Reply to the source message; Get is Feishu's 收到 reaction."""
    request = BaseRequest()
    request.http_method = HttpMethod.POST
    endpoint = "reactions" if kind == "received" else "reply"
    request.uri = f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/{endpoint}"
    request.token_types = {AccessTokenType.TENANT}
    request.headers["Content-Type"] = "application/json; charset=utf-8"
    request.body = (
        {"reaction_type": {"emoji_type": "Get"}}
        if kind == "received"
        else {
            "msg_type": "text",
            "content": json.dumps({"text": reason}, ensure_ascii=False),
            "uuid": str(
                uuid.uuid5(uuid.NAMESPACE_URL, "scout:zhihu:receipt:" + message_id)
            ),
        }
    )
    response = _build_client(resolve_feishu_delivery(), 15).request(request)
    if not response.success():
        raise ReceiptDeliveryError(response)


def deliver_receipt(database):
    # Claim before HTTP, without holding the general card sender lock.
    with transaction(database, rows=True, timeout=0.3) as conn:
        conn.execute(
            "UPDATE zhihu_message_receipts SET status='failed' "
            "WHERE status='sending' AND attempts>=3 AND retry_at<=?",
            (time.time(),),
        )
        row = conn.execute(
            "SELECT * FROM zhihu_message_receipts WHERE status IN ('pending','sending') "
            "AND attempts<3 AND retry_at<=? ORDER BY rowid LIMIT 1",
            (time.time(),),
        ).fetchone()
        if row is None:
            return
        attempt = row["attempts"] + 1
        conn.execute(
            "UPDATE zhihu_message_receipts SET status='sending',attempts=?,retry_at=? WHERE message_id=?",
            (attempt, time.time() + 60, row["message_id"]),
        )
    try:
        send_receipt(row["message_id"], row["kind"], row["reason"])
    except Exception as exc:  # noqa: BLE001 - persist transport failures for retry
        with transaction(database) as conn:
            conn.execute(
                "UPDATE zhihu_message_receipts SET status=?,retry_at=?,last_error=? WHERE message_id=?",
                (
                    "failed" if attempt == 3 else "pending",
                    time.time() + (5 if attempt == 1 else 15),
                    _receipt_error(exc),
                    row["message_id"],
                ),
            )
        logger.warning("Zhihu receipt delivery failed: %s", _receipt_error(exc))
    else:
        with transaction(database) as conn:
            conn.execute(
                "UPDATE zhihu_message_receipts SET status='delivered',last_error='' WHERE message_id=?",
                (row["message_id"],),
            )


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
                deliver_receipt(self.database)
            except Exception as exc:  # noqa: BLE001 - durable outbox retries on next tick
                logger.warning("Zhihu receipt delivery failed: %s", type(exc).__name__)
            self.wake.wait(1)
