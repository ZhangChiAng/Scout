"""Fast SQLite-backed Feishu card feedback callbacks over long connection."""

from __future__ import annotations

import asyncio
import base64
import http
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import lark_oapi as lark
from lark_oapi.core.const import UTF_8
from lark_oapi.core.json import JSON
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.ws.client import Client as _WSClient
from lark_oapi.ws.client import _get_by_key
from lark_oapi.ws.const import (
    HEADER_BIZ_RT,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
)
from lark_oapi.ws.enum import MessageType
from lark_oapi.ws.model import Response

from .config import FeishuDeliveryConfig
from .issue_state import IssueState
from .model import PersonalizedEvaluation
from .notifier import (
    FeishuNotifier,
    build_article_card,
    build_feedback_form_card,
    build_recorded_card,
)
from .reveal import RevealWorker
from .storage import FeedbackError, SQLiteStorage


class FeedbackListenerError(RuntimeError):
    """Raised when the long-connection listener cannot be started."""


class _CardCompatibleWSClient(_WSClient):
    """Dispatch CARD frames on SDK versions that only dispatch EVENT frames.

    lark-oapi 1.7.2 accepts ``register_p2_card_action_trigger`` but returns
    before invoking it for long-connection CARD frames.  Keeping this tiny
    compatibility override local also works after the upstream fix lands.
    """

    async def _handle_data_frame(self, frame: Any) -> None:
        headers = frame.headers
        message_type = MessageType(_get_by_key(headers, HEADER_TYPE))
        if message_type != MessageType.CARD:
            await super()._handle_data_frame(frame)
            return

        message_id = _get_by_key(headers, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(headers, HEADER_TRACE_ID)
        parts = int(_get_by_key(headers, HEADER_SUM))
        sequence = int(_get_by_key(headers, HEADER_SEQ))
        payload = frame.payload
        if parts > 1:
            payload = self._combine(message_id, parts, sequence, payload)
            if payload is None:
                return

        response = Response(code=http.HTTPStatus.OK)
        try:
            started = round(time.time() * 1000)
            result = self._event_handler._do_without_validation(payload)
            elapsed = round(time.time() * 1000) - started
            header = headers.add()
            header.key = HEADER_BIZ_RT
            header.value = str(elapsed)
            if result is not None:
                response.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as exc:  # noqa: BLE001 - return callback failure frame
            lark.logger.error(
                "Scout card callback failed: trace_id=%s error=%s",
                trace_id,
                type(exc).__name__,
            )
            response = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)
        frame.payload = JSON.marshal(response).encode(UTF_8)
        await self._write_message(frame.SerializeToString())


class FeedbackHandler:
    def __init__(
        self,
        storage: SQLiteStorage,
        *,
        max_payload_bytes: int,
        wake_reveal: Callable[[], None],
    ) -> None:
        self.storage = storage
        self.max_payload_bytes = max_payload_bytes
        self.issue_state = IssueState(storage)
        self.wake_reveal = wake_reveal

    def __call__(self, callback: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            return self._handle(callback)
        except FeedbackError as exc:
            return _response(toast=str(exc), toast_type="error")
        except Exception as exc:  # noqa: BLE001 - keep callback latency bounded
            lark.logger.exception(
                "Scout feedback callback failed: %s", type(exc).__name__
            )
            return _response(toast="反馈处理失败，请稍后重试", toast_type="error")

    def _handle(self, callback: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        event = callback.event
        action = event.action if event is not None else None
        context = event.context if event is not None else None
        operator = event.operator if event is not None else None
        value = (
            action.value
            if action is not None and isinstance(action.value, dict)
            else {}
        )
        message_id = context.open_message_id if context is not None else ""
        open_id = operator.open_id if operator is not None else ""
        if value.get("action") == "reveal_selected":
            count = self.issue_state.enqueue(
                list_id=_positive_int(value.get("list_id"), "list_id"),
                message_id=message_id or "",
                chat_id=context.open_chat_id if context is not None else "",
                open_id=open_id or "",
                event_id=getattr(callback.header, "event_id", "") or "",
                form=action.form_value,
            )
            self.wake_reveal()
            logging.getLogger(__name__).info(
                "Reveal submission list=%s queued=%s", value.get("list_id"), count
            )
            return _response(
                toast=f"已接收 {count} 条正文请求"
                if count
                else "所选正文已在发送或已送达",
                toast_type="success",
            )
        snapshot_id = _positive_int(value.get("snapshot_id"), "snapshot_id")
        purpose = value.get("purpose")
        if purpose not in {"calibration", "personalized"}:
            raise FeedbackError("反馈卡片类型无效")
        delivery = self.storage.get_card_delivery_by_snapshot(snapshot_id, purpose)
        if delivery is None or delivery.message_id != message_id:
            raise FeedbackError("找不到对应的卡片送达记录")

        action_name = value.get("action")
        if action_name == "select":
            sentiment = _sentiment(value.get("sentiment"))
            card = build_feedback_form_card(
                delivery,
                sentiment=sentiment,
                max_payload_bytes=self.max_payload_bytes,
            )
            return _response(card=card)
        if action_name == "edit":
            latest = self.storage.latest_feedback_for_delivery(delivery.delivery_id)
            evaluation = PersonalizedEvaluation(
                delivery.article.article_key, delivery.verdict, delivery.reason
            )
            card = build_article_card(
                delivery.article,
                evaluation,
                snapshot_id=delivery.snapshot_id,
                purpose=delivery.purpose,
                max_payload_bytes=self.max_payload_bytes,
                feedback=latest,
            )
            return _response(card=card, toast="请选择新的倾向")
        if action_name != "submit":
            raise FeedbackError("未知的反馈操作")

        sentiment = _sentiment(value.get("sentiment"))
        form = action.form_value if action is not None else None
        reason_value = form.get("reason") if isinstance(form, dict) else ""
        reason = reason_value.strip() if isinstance(reason_value, str) else ""
        if not 1 <= len(reason) <= 500:
            card = build_feedback_form_card(
                delivery,
                sentiment=sentiment,
                max_payload_bytes=self.max_payload_bytes,
                error="原因不能为空，且最多 500 字。",
            )
            return _response(card=card, toast="请填写 1–500 字原因", toast_type="error")
        write = self.storage.record_feedback(
            delivery_id=delivery.delivery_id,
            message_id=message_id,
            open_id=open_id or "",
            sentiment=sentiment,
            reason=reason,
        )
        latest = self.storage.latest_feedback_for_delivery(delivery.delivery_id)
        if latest is None:
            raise FeedbackError("反馈已写入但无法读取")
        card = build_recorded_card(
            delivery, latest, max_payload_bytes=self.max_payload_bytes
        )
        toast = "反馈已记录" if write.created else "相同反馈已记录，无需重复提交"
        return _response(card=card, toast=toast, toast_type="success")


def listen_feedback(
    delivery: FeishuDeliveryConfig,
    *,
    database_path: str | Path,
    max_payload_bytes: int,
    timeout_seconds: float,
) -> None:
    storage = SQLiteStorage(database_path)
    storage.initialize()
    worker = RevealWorker(
        IssueState(storage),
        FeishuNotifier(delivery, timeout_seconds),
        max_payload_bytes=max_payload_bytes,
    )
    callback = FeedbackHandler(
        storage,
        max_payload_bytes=max_payload_bytes,
        wake_reveal=worker.wake.set,
    )
    dispatcher = (
        lark.EventDispatcherHandler.builder("", "", lark.LogLevel.ERROR)
        .register_p2_card_action_trigger(callback)
        .build()
    )
    client = _CardCompatibleWSClient(
        delivery.app_id,
        delivery.app_secret,
        event_handler=dispatcher,
        log_level=lark.LogLevel.ERROR,
    )
    try:
        worker.start()
    except BlockingIOError as exc:
        raise FeedbackListenerError(
            "another Scout listener is already running"
        ) from exc
    try:
        client.start()
    finally:
        worker.close()
        loop = asyncio.get_event_loop()
        if not loop.is_closed():
            loop.run_until_complete(client._disconnect())
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )


def _response(
    *,
    card: dict[str, object] | None = None,
    toast: str = "",
    toast_type: str = "info",
) -> P2CardActionTriggerResponse:
    payload: dict[str, object] = {}
    if toast:
        payload["toast"] = {"type": toast_type, "content": toast}
    if card is not None:
        payload["card"] = {"type": "raw", "data": card}
    return P2CardActionTriggerResponse(payload)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise FeedbackError(f"{name} 无效")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise FeedbackError(f"{name} 无效") from exc
    if result <= 0:
        raise FeedbackError(f"{name} 无效")
    return result


def _sentiment(value: object) -> str:
    if value not in {"like", "dislike"}:
        raise FeedbackError("反馈倾向无效")
    return str(value)
