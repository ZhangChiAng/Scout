"""知乎话题配置、同题回答扩展与反馈学习。"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from .config import ConfigError, load_config, load_dotenv, resolve_feishu_delivery
from .locking import RunLockedError, sender_lock
from .notifier import NotificationError
from .zhihu_client import CollectorClient, CollectorError
from .zhihu_delivery import delivery_summary, initialize, reset_failed
from .zhihu_scan import (
    export_report,
    get_scan,
    resume_collection,
    resume_pending,
    summary,
)


def _state(database, run_id):
    state = get_scan(database, run_id)
    if state is None:
        raise ConfigError("找不到知乎扫描任务")
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "查看采集器、登录、扫描和通知状态"),
        ("scan", "扫描话题并推送内容卡片；--run-id 恢复原扫描"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--database", type=Path)
        sub.add_argument("--run-id", help="Scout 扫描 UUID")
        if name == "scan":
            sub.add_argument("--topic-id", type=int, help="从飞书管理卡片保存的话题 ID")
            sub.add_argument(
                "--request-uuid", help="相同 UUID 返回原扫描，不创建新的发送授权"
            )
            sub.add_argument("--wait-seconds", type=float, default=60)
    for name in ("continue", "append", "stop"):
        sub = commands.add_parser(
            name, help="逐批控制语义扫描；完成后等待反馈及明确继续"
        )
        sub.add_argument("--database", type=Path)
        sub.add_argument("--run-id", required=True)
        sub.add_argument("--wait-seconds", type=float, default=60)
        if name == "append":
            sub.add_argument("--quantity", type=int, required=True)
        if name != "stop":
            sub.add_argument(
                "--parameter",
                choices=(
                    "evaluation_batch_size",
                    "search_latest_pages_per_advance",
                    "search_general_pages_per_advance",
                    "question_pages_per_advance",
                ),
            )
            sub.add_argument("--value", type=int)
            sub.add_argument("--advance-collection", action="store_true")
    for name, help_text in (
        ("manage", "发送知乎话题管理卡片"),
        ("topics", "查看话题和语义偏好学习进度"),
        ("retry-cards", "恢复失败的管理或补标卡片，复用原 UUID"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--database", type=Path)
    args = parser.parse_args(argv)
    try:
        load_dotenv()
        database = args.database or Path(
            os.environ.get("SCOUT_DB_PATH", "data/scout.sqlite3")
        )
        if args.command in {"continue", "append", "stop"}:
            from .zhihu_semantic_scan import request_control

            if not 0 <= args.wait_seconds <= 3600:
                raise ConfigError("wait-seconds 必须在 0..3600 内")
            parameter = getattr(args, "parameter", None)
            value = getattr(args, "value", None)
            if (parameter is None) != (value is None):
                raise ConfigError("parameter 和 value 需同时填写")
            request = request_control(
                database,
                args.run_id,
                args.command,
                quantity=getattr(args, "quantity", None),
                parameter_overrides={parameter: value} if parameter else None,
                advance_collection=getattr(args, "advance_collection", False)
                or parameter
                in {
                    "search_latest_pages_per_advance",
                    "search_general_pages_per_advance",
                    "question_pages_per_advance",
                },
            )
            if args.command == "stop":
                print(
                    json.dumps(
                        {
                            "scan_id": request["scan_id"],
                            "control": request,
                            "stop_requested": True,
                        }
                    )
                )
                return 0
            deadline = time.monotonic() + args.wait_seconds
            while time.monotonic() < deadline:
                resume_pending(database)
                state = _state(database, args.run_id)
                if state["status"] not in {"running", "stopping"}:
                    break
                time.sleep(min(2, max(0, deadline - time.monotonic())))
            print(
                json.dumps(
                    summary(_state(database, args.run_id)), ensure_ascii=False, indent=2
                )
            )
            return 0
        if args.command in {"manage", "topics", "retry-cards"}:
            from . import zhihu_learning, zhihu_store
            from .database import transaction
            from .zhihu_actions import ZhihuActionHandler
            from .zhihu_workflow import work

            with sender_lock(database):
                initialize(database)
                zhihu_store.initialize(database)
            if args.command == "manage":
                delivery = resolve_feishu_delivery()
                if delivery.receive_id_type != "chat_id":
                    raise ConfigError("知乎管理卡片必须发送到飞书群 chat_id")
                card = ZhihuActionHandler(
                    database, max_payload_bytes=load_config().feishu.max_payload_bytes
                ).management_card()
                queued = zhihu_store.enqueue_card(database, card, delivery.receive_id)
                work(database)
                print(
                    json.dumps(
                        {
                            "management_card_id": queued["id"],
                            "send_uuid": queued["send_uuid"],
                        },
                        ensure_ascii=False,
                    )
                )
            elif args.command == "retry-cards":
                with sender_lock(database), transaction(database) as conn:
                    changed = conn.execute(
                        "UPDATE zhihu_card_deliveries SET status='pending',attempts=0,retry_at=0,last_error='' WHERE status='failed' OR (status='sending' AND attempts>=3)"
                    ).rowcount
                work(database)
                print(json.dumps({"resumed": changed}))
            else:
                print(
                    json.dumps(
                        {
                            "topics": zhihu_store.list_topics(database),
                            "schedule": zhihu_store.get_schedule(database),
                            "learning": zhihu_learning.snapshot(database),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return 0
        if args.command == "status":
            state = get_scan(database, args.run_id) if database.exists() else None
            client = CollectorClient.from_env()
            health, login = client.health(), client.login_status()
            print(
                json.dumps(
                    {
                        "collector": health,
                        "login": {
                            key: login.get(key)
                            for key in ("status", "auth_method", "verified_at", "error")
                        },
                        "scan": summary(state) if state else None,
                        "notifications": delivery_summary(database, state["id"])
                        if state
                        else {},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if not 0 <= args.wait_seconds <= 3600:
            raise ConfigError("wait-seconds 必须在 0..3600 内")
        if args.run_id and args.request_uuid:
            raise ConfigError("run-id 和 request-uuid 不能同时使用")
        if args.run_id and args.topic_id:
            raise ConfigError("run-id 和 topic-id 不能同时使用")
        if args.run_id:
            state = _state(database, args.run_id)
            with sender_lock(database):
                initialize(database)
            reset_failed(database, state["id"])
            resume_collection(database, state["id"])
            state = _state(database, state["id"])
        else:
            if not args.topic_id:
                raise ConfigError("新扫描需要 --topic-id；先用 manage 在飞书配置话题")
            from .zhihu_semantic_scan import create_scan as create_topic_scan

            state = create_topic_scan(database, args.topic_id, args.request_uuid)
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            resume_pending(database)
            state = _state(database, state["id"])
            notifications = delivery_summary(database, state["id"])
            if state["status"] != "running" and not any(
                notifications.get(s, 0) for s in ("pending", "sending")
            ):
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        export_report(state)
        notifications = delivery_summary(database, state["id"])
        print(
            json.dumps(
                {**summary(state), "notifications": notifications},
                ensure_ascii=False,
                indent=2,
            )
        )
        return (
            0
            if state["status"] in {"completed", "waiting_user", "exhausted"}
            and not any(
                notifications.get(s, 0) for s in ("pending", "sending", "failed")
            )
            else 2
        )
    except (
        ConfigError,
        CollectorError,
        NotificationError,
        RunLockedError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
