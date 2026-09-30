"""Background semantic scan jobs, learning, and management/review card delivery."""

import fcntl
import json
import logging
import time
import uuid
from contextlib import closing
from pathlib import Path

from . import zhihu_delivery, zhihu_learning, zhihu_store
from .config import ConfigError, resolve_feishu_delivery
from .database import connect, transaction
from .locking import RunLockedError, sender_lock
from .notifier import FeishuNotifier, NotificationError
from .zhihu_client import CollectorError

logger = logging.getLogger(__name__)


def work(database):
    path = Path(database)
    with path.with_suffix(path.suffix + ".zhihu-workflow.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            with sender_lock(path):
                zhihu_delivery.initialize(path)
                zhihu_store.initialize(path)
        except RunLockedError:
            return
        _refresh_feedback_reports(path)
        _jobs(path)
        _cards(path)


def _refresh_feedback_reports(database):
    from .zhihu_semantic_scan import _save, export_report, refresh_feedback_stats

    path = Path(database)
    with path.with_suffix(path.suffix + ".zhihu-scan.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        learning = zhihu_learning.snapshot(path)
        with closing(connect(path, read_only=True)) as conn:
            rows = conn.execute(
                "SELECT state_json FROM zhihu_scans WHERE json_extract(state_json,'$.schema_version')=5"
            ).fetchall()
        for row in rows:
            state = json.loads(row[0])
            if state.get("learning") != learning:
                refresh_feedback_stats(path, state)
                _save(path, state)
                export_report(state)


def _jobs(database):
    from .zhihu_semantic_scan import create_scan

    for job in zhihu_store.pending_jobs(database, kind="scan")[:5]:
        if (
            str(job.get("event_id") or "").startswith("daily:")
            and job["status"] == "pending"
        ):
            with transaction(database) as conn:
                conn.execute(
                    "UPDATE zhihu_jobs SET status='cancelled',last_error='每日扫描已停用' WHERE id=?",
                    (job["id"],),
                )
            continue
        zhihu_store.start_job(database, job["id"])
        try:
            topic_id = job["payload"].get("topic_id")
            topics = (
                [zhihu_store.get_topic(database, topic_id)]
                if topic_id is not None
                else zhihu_store.list_topics(database, enabled_only=True)
            )
            if not topics or any(topic is None for topic in topics):
                raise ConfigError("没有可扫描的话题")
            for topic in topics:
                scan_uuid = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"scout:zhihu:job:{job['id']}:topic:{topic['id']}",
                    )
                )
                create_scan(database, topic["id"], scan_uuid)
        except RunLockedError:
            return
        except (CollectorError, ConfigError, ValueError) as exc:
            if isinstance(exc, CollectorError) and exc.kind in {
                "network",
                "http_error",
            }:
                # Keep the original job and derived scan UUID through collector downtime.
                with transaction(database) as conn:
                    conn.execute(
                        "UPDATE zhihu_jobs SET last_error=? WHERE id=?",
                        (str(exc)[:300], job["id"]),
                    )
                return
            zhihu_store.finish_job(database, job["id"], error=str(exc)[:300])
        else:
            zhihu_store.finish_job(database, job["id"])


def _cards(database, *, messages_only=False):
    try:
        with sender_lock(database):
            _send_card(database, messages_only=messages_only)
    except RunLockedError:
        return


def _send_card(database, *, messages_only=False):
    with closing(connect(database, read_only=True, rows=True)) as conn:
        row = conn.execute(
            """SELECT * FROM zhihu_card_deliveries d
            WHERE status IN ('pending','sending') AND retry_at<=?
            AND (?=0 OR event_id LIKE 'message:%')
            AND NOT (event_id LIKE 'semantic:%' AND EXISTS (
                SELECT 1 FROM zhihu_settings s
                WHERE s.key='semantic_stop:' || substr(d.event_id,10,36)))
            ORDER BY CASE WHEN event_id LIKE 'message:%' THEN 0 ELSE 1 END, id LIMIT 1""",
            (time.time(), int(messages_only)),
        ).fetchone()
    if row is None:
        return
    if row["attempts"] >= 3:
        with transaction(database) as conn:
            conn.execute(
                "UPDATE zhihu_card_deliveries SET status='failed' WHERE id=?",
                (row["id"],),
            )
        return
    attempt = row["attempts"] + 1
    with transaction(database) as conn:
        conn.execute(
            "UPDATE zhihu_card_deliveries SET status='sending',attempts=?,retry_at=? WHERE id=?",
            (attempt, time.time() + (5 if attempt == 1 else 15), row["id"]),
        )
    try:
        notifier = FeishuNotifier(resolve_feishu_delivery(), 15)
        sent = notifier.send_card(
            json.loads(row["card_json"]),
            chat_id=row["chat_id"],
            send_uuid=row["send_uuid"],
        )
        if sent.chat_id != row["chat_id"]:
            raise NotificationError("返回群与固定目标群不一致")
    except (NotificationError, ConfigError) as exc:
        with transaction(database) as conn:
            conn.execute(
                "UPDATE zhihu_card_deliveries SET status=?,last_error=? WHERE id=?",
                ("failed" if attempt == 3 else "pending", str(exc)[:300], row["id"]),
            )
    else:
        with transaction(database) as conn:
            conn.execute(
                "UPDATE zhihu_card_deliveries SET status='delivered',message_id=?,delivered_at=?,last_error='' WHERE id=?",
                (sent.message_id, time.time(), row["id"]),
            )
