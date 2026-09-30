"""Short, SQLite-only operations for Zhihu Feishu card callbacks."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import zhihu_learning, zhihu_store
from .database import connect
from .storage import FeedbackError
from .zhihu_cards import (
    FILTERED_PER_PAGE,
    build_append_form_card,
    build_content_card,
    build_feedback_form_card,
    build_filtered_card,
    build_management_card,
    build_pending_card,
    build_stream_card,
    build_topic_form_card,
)

KNOWN_ACTIONS = {
    "zhihu_manage",
    "zhihu_topic_new",
    "zhihu_topic_edit",
    "zhihu_topic_save",
    "zhihu_topic_toggle",
    "zhihu_schedule",
    "zhihu_schedule_save",
    "zhihu_schedule_disable",
    "zhihu_scan",
    "zhihu_scan_status",
    "zhihu_scan_pending",
    "zhihu_scan_continue",
    "zhihu_scan_append",
    "zhihu_scan_append_submit",
    "zhihu_scan_stop",
    "zhihu_filtered",
    "zhihu_review",
    "zhihu_like",
    "zhihu_like_submit",
    "zhihu_dislike",
    "zhihu_dislike_submit",
    "zhihu_feedback_edit",
}
STATUS_LABELS = {
    "pending": "待处理",
    "running": "进行中",
    "waiting_login": "等待知乎登录",
    "completed": "已完成",
    "partial_failed": "部分失败，覆盖不完整",
    "failed": "失败",
    "waiting_user": "等待继续查找",
    "paused": "已暂停",
    "stopped": "本轮已结束",
    "exhausted": "结果实际耗尽",
    "waiting_model": "等待模型空闲",
    "model_busy": "等待模型空闲",
    "stopping": "正在停止",
    "cancelled": "已取消",
    "model_failed": "模型失败",
    "collector_failed": "采集失败",
    "processing_failed": "处理异常",
    "delivery_failed": "发送失败",
}


def _activity_time(stamp: str) -> str:
    try:
        return (
            datetime.fromisoformat(stamp)
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .strftime("%m-%d %H:%M")
        )
    except ValueError:
        return stamp[:16]


def _integer(value: object, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise FeedbackError("卡片编号无效")
    try:
        result = int(str(value))
    except (ValueError, TypeError) as exc:
        raise FeedbackError("卡片编号无效") from exc
    if result < minimum:
        raise FeedbackError("卡片编号无效")
    return result


def _field(form: dict, name: str) -> str:
    value = form.get(name, "")
    if not isinstance(value, str):
        raise FeedbackError("表单字段无效")
    return value.strip()


class ZhihuActionHandler:
    """Persist user operations; the background worker collects, trains and sends."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        max_payload_bytes: int,
        wake: Callable[[], None] | None = None,
    ) -> None:
        self.database_path = database_path
        self.max_payload_bytes = max_payload_bytes
        self.wake = wake or (lambda: None)

    def management_card(self, offset: int = 0) -> dict:
        topics = zhihu_store.list_topics(self.database_path)
        with closing(
            connect(self.database_path, read_only=True, rows=True, timeout=0.3)
        ) as conn:
            for topic in topics:
                row = conn.execute(
                    """SELECT id,json_extract(state_json,'$.query_plan') AS plan
                    FROM zhihu_scans
                    WHERE json_extract(state_json,'$.schema_version') =5
                    AND json_extract(state_json,'$.topic.id')=?
                    ORDER BY created_at DESC LIMIT 1""",
                    (topic["id"],),
                ).fetchone()
                if row:
                    topic["latest_scan_id"] = row["id"]
                    topic["latest_query_plan"] = json.loads(row["plan"] or "[]")
        return build_management_card(
            topics,
            zhihu_store.get_schedule(self.database_path),
            zhihu_learning.snapshot(self.database_path),
            offset=offset,
            recent_activity=self._recent_activity(),
            max_payload_bytes=self.max_payload_bytes,
        )

    def _recent_activity(self) -> list[str]:
        """Read only small status columns; scan JSON can contain thousands of bodies."""
        with closing(
            connect(self.database_path, read_only=True, rows=True, timeout=0.3)
        ) as conn:
            jobs = conn.execute(
                "SELECT status,last_error,created_at FROM zhihu_jobs WHERE kind='scan' ORDER BY id DESC LIMIT 3"
            ).fetchall()
            scans = conn.execute(
                "SELECT status,created_at FROM zhihu_scans ORDER BY created_at DESC LIMIT 3"
            ).fetchall()
        messages = []
        for job in jobs:
            label = (
                "扫描已安排"
                if job["status"] == "completed"
                else f"扫描请求{STATUS_LABELS.get(job['status'], job['status'])}"
            )
            error = f"：{job['last_error'][:240]}" if job["last_error"] else ""
            messages.append(f"{_activity_time(job['created_at'])} · {label}{error}")
        for scan in scans:
            label = STATUS_LABELS.get(scan["status"], scan["status"])
            messages.append(f"{_activity_time(scan['created_at'])} · 扫描{label}")
        return messages

    def handle(
        self,
        value: dict,
        *,
        form: dict | None,
        message_id: str,
        chat_id: str,
        open_id: str,
        event_id: str,
    ) -> dict:
        try:
            return self._handle(
                value,
                form=form or {},
                message_id=message_id,
                chat_id=chat_id,
                open_id=open_id,
                event_id=event_id,
            )
        except ValueError as exc:
            raise FeedbackError(str(exc)) from exc

    def _handle(
        self,
        value: dict,
        *,
        form: dict,
        message_id: str,
        chat_id: str,
        open_id: str,
        event_id: str,
    ) -> dict:
        database = self.database_path
        action = value.get("action")
        if not isinstance(action, str) or action not in KNOWN_ACTIONS:
            raise FeedbackError("未知的知乎操作")
        # Only a delivered Scout card may bind the sole owner on its first action.
        zhihu_store.authorize_card(database, message_id, chat_id)
        zhihu_store.authorize_owner(database, open_id)
        if action == "zhihu_manage":
            card = self.management_card(_integer(value.get("offset", 0), minimum=0))
            if value.get("separate") is True:
                self._require_event(event_id)
                zhihu_store.enqueue_card(database, card, chat_id, event_id=event_id)
                self.wake()
                return {"toast": "话题管理卡片即将发送到群中", "toast_type": "success"}
            return {"card": card}
        if action == "zhihu_topic_new":
            return {
                "card": build_topic_form_card(max_payload_bytes=self.max_payload_bytes)
            }
        if action == "zhihu_topic_edit":
            topic = self._topic(value)
            return {
                "card": build_topic_form_card(
                    topic, max_payload_bytes=self.max_payload_bytes
                )
            }
        if action == "zhihu_topic_save":
            self._require_event(event_id)
            topic = self._topic(value) if value.get("topic_id") is not None else None
            saved = zhihu_store.save_topic(
                database,
                name=_field(form, "name"),
                description=_field(form, "description"),
                time_range="30d",
                topic_id=topic["id"] if topic else None,
                enabled=True,
                event_id=event_id,
            )
            zhihu_store.enqueue_job(
                database,
                "scan",
                {"topic_id": saved["id"]},
                event_id="topic-start:" + event_id,
            )
            self.wake()
            return {
                "card": self.management_card(),
                "toast": "话题已保存，新一轮扫描已安排",
                "toast_type": "success",
            }
        if action == "zhihu_topic_toggle":
            topic = self._topic(value)
            enabled = value.get("enabled")
            if type(enabled) is not bool:
                raise FeedbackError("话题启用状态无效")
            zhihu_store.set_topic_enabled(database, topic["id"], enabled)
            return {
                "card": self.management_card(),
                "toast": "话题已启用" if enabled else "话题已停用",
            }
        if action in {
            "zhihu_schedule",
            "zhihu_schedule_save",
            "zhihu_schedule_disable",
        }:
            raise FeedbackError("每日扫描已停用，请从话题管理主动开始新一轮")
        if action in {
            "zhihu_scan_status",
            "zhihu_scan_pending",
            "zhihu_scan_continue",
            "zhihu_scan_append",
            "zhihu_scan_append_submit",
            "zhihu_scan_stop",
        }:
            scan = self._scan(value)
            if action == "zhihu_scan_pending":
                return {
                    "card": build_pending_card(
                        scan,
                        offset=_integer(value.get("offset", 0), minimum=0),
                        max_payload_bytes=self.max_payload_bytes,
                    )
                }
            if action == "zhihu_scan_append":
                return {
                    "card": build_append_form_card(
                        scan["id"], max_payload_bytes=self.max_payload_bytes
                    )
                }
            if action == "zhihu_scan_status":
                return {"card": self._scan_card(scan)}
            self._require_event(event_id)
            from .zhihu_semantic_scan import request_control

            operation = {
                "zhihu_scan_continue": "continue",
                "zhihu_scan_append_submit": "append",
                "zhihu_scan_stop": "stop",
            }[action]
            quantity = None
            if operation == "append":
                raw_quantity = _field(form, "quantity")
                if not raw_quantity.isascii() or not raw_quantity.isdecimal():
                    raise FeedbackError("追加数量需为正整数")
                quantity = int(raw_quantity)
                if not 1 <= quantity <= 10000:
                    raise FeedbackError("追加数量需为 1–10000 的整数")
            request_control(
                database,
                scan["id"],
                operation,
                quantity=quantity,
                event_id=event_id,
            )
            self.wake()
            return {
                "card": self._scan_card(self._scan(value)),
                "toast": "本轮停止请求已保存，在途结果会保留"
                if operation == "stop"
                else "继续请求已保存，使用最新有效偏好继续查找",
                "toast_type": "success",
            }
        if action == "zhihu_scan":
            self._require_event(event_id)
            payload = {}
            if value.get("topic_id") is not None:
                topic = self._topic(value)
                if not topic["enabled"]:
                    raise FeedbackError("请先启用该话题")
                payload["topic_id"] = topic["id"]
            elif not zhihu_store.list_topics(database, enabled_only=True):
                raise FeedbackError("请先新增或启用话题")
            zhihu_store.enqueue_job(database, "scan", payload, event_id=event_id)
            self.wake()
            return {
                "toast": "扫描请求已保存，后台完成后推送新内容",
                "toast_type": "success",
            }
        if action == "zhihu_filtered":
            offset = _integer(value.get("offset", 0), minimum=0)
            topic_id = (
                self._topic(value)["id"] if value.get("topic_id") is not None else None
            )
            scan_id = None
            if value.get("scan_id") is not None:
                scan = self._scan(value)
                scan_id = scan["id"]
                if topic_id is not None and topic_id != scan["topic"]["id"]:
                    raise FeedbackError("话题与扫描轮次不一致")
                topic_id = scan["topic"]["id"]
            rows = zhihu_store.list_filtered(
                database,
                topic_id=topic_id,
                limit=FILTERED_PER_PAGE + 1,
                offset=offset,
                scan_id=scan_id,
            )
            return {
                "card": build_filtered_card(
                    rows,
                    offset=offset,
                    topic_id=topic_id,
                    scan_id=scan_id,
                    max_payload_bytes=self.max_payload_bytes,
                )
            }
        if action == "zhihu_review":
            self._require_event(event_id)
            article = self._content(value)
            feedback = zhihu_store.latest_feedback(database, article["article_key"])
            card = build_content_card(
                article,
                snapshot_id=article["snapshot_id"],
                feedback=feedback,
                reason=article.get("_review_reason") or "来自结果复核的补充反馈",
                max_payload_bytes=self.max_payload_bytes,
            )
            zhihu_store.enqueue_card(
                database,
                card,
                chat_id,
                event_id=event_id,
                snapshot_id=article["snapshot_id"],
            )
            self.wake()
            return {
                "toast": "已保存查看请求，内容卡片即将发送到群中",
                "toast_type": "success",
            }
        if action not in {
            "zhihu_like",
            "zhihu_like_submit",
            "zhihu_dislike",
            "zhihu_dislike_submit",
            "zhihu_feedback_edit",
        }:
            raise FeedbackError("未知的知乎操作")
        article = self._content(value)
        snapshot_id = article["snapshot_id"]
        zhihu_store.authorize_card(
            database, message_id, chat_id, snapshot_id=snapshot_id
        )
        if action == "zhihu_feedback_edit":
            return {
                "card": build_content_card(
                    article,
                    snapshot_id=snapshot_id,
                    max_payload_bytes=self.max_payload_bytes,
                )
            }
        if action in {"zhihu_like", "zhihu_dislike"}:
            feedback = zhihu_store.latest_feedback(database, article["article_key"])
            return {
                "card": build_feedback_form_card(
                    article,
                    label="like" if action == "zhihu_like" else "dislike",
                    snapshot_id=snapshot_id,
                    feedback=feedback,
                    max_payload_bytes=self.max_payload_bytes,
                )
            }
        self._require_event(event_id)
        zhihu_store.save_feedback(
            database,
            snapshot_id=snapshot_id,
            label="like" if action == "zhihu_like_submit" else "dislike",
            reason=_field(form, "reason"),
            event_id=event_id,
            message_id=message_id,
        )
        self.wake()
        feedback = zhihu_store.latest_feedback(database, article["article_key"])
        return {
            "card": build_content_card(
                article,
                snapshot_id=snapshot_id,
                feedback=feedback,
                learning=zhihu_learning.snapshot(database),
                max_payload_bytes=self.max_payload_bytes,
            ),
            "toast": "反馈已保存，偏好更新中；明确继续后用于下次搜索或继续",
            "toast_type": "success",
        }

    def _topic(self, value: dict) -> dict:
        topic = zhihu_store.get_topic(
            self.database_path, _integer(value.get("topic_id"))
        )
        if topic is None:
            raise FeedbackError("该话题不存在")
        return topic

    def _scan(self, value: dict) -> dict:
        from .zhihu_scan import get_scan

        try:
            scan_id = str(uuid.UUID(str(value.get("scan_id", ""))))
        except (ValueError, TypeError, AttributeError) as exc:
            raise FeedbackError("扫描编号无效") from exc
        scan = get_scan(self.database_path, scan_id)
        if not scan:
            raise FeedbackError("找不到对应的语义扫描，请开始新一轮")
        if scan.get("schema_version") != 5:
            raise FeedbackError("旧扫描已终止，请重新发起搜索")
        return scan

    def _scan_card(self, scan: dict) -> dict:
        from .zhihu_semantic_scan import refresh_feedback_stats

        refresh_feedback_stats(self.database_path, scan)
        return build_stream_card(scan, max_payload_bytes=self.max_payload_bytes)

    def _content(self, value: dict) -> dict:
        article = zhihu_store.get_content(
            self.database_path, snapshot_id=_integer(value.get("snapshot_id"))
        )
        if article is None:
            raise FeedbackError("找不到对应的知乎正文快照")
        # Evaluation may have completed after the immutable body snapshot was saved.
        scan_id = self._scan(value)["id"] if value.get("scan_id") is not None else None
        query = (
            "SELECT article_json,reason,coalesce((SELECT json_extract(s.state_json,'$.evaluation_policy') "
            "FROM zhihu_scans s WHERE s.id=r.scan_id),'historical') AS evaluation_policy "
            "FROM zhihu_candidate_results r WHERE snapshot_id=?"
        )
        args = [article["snapshot_id"]]
        if scan_id:
            query += " AND scan_id=?"
            args.append(scan_id)
        with closing(connect(self.database_path, read_only=True, timeout=0.3)) as conn:
            row = conn.execute(
                query + " ORDER BY updated_at DESC LIMIT 1", args
            ).fetchone()
        if scan_id and row is None:
            raise FeedbackError("该内容不属于当前轮次的复核结果")
        if row:
            evaluated = json.loads(row[0])
            article["_review_reason"] = row[1]
            article["evaluation_policy"] = row[2]
            for field in (
                "relevance",
                "evaluation",
                "stages",
                "semantic_result",
                "semantic",
                "summary",
                "decision",
                "decision_reason",
            ):
                article.pop(field, None)
                if field in evaluated:
                    article[field] = evaluated[field]
        return article

    @staticmethod
    def _require_event(event_id: str) -> None:
        if not event_id:
            raise FeedbackError("回调缺少事件编号，请重新打开卡片后重试")
