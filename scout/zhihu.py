"""Local Zhihu collection, review and explicitly scoped result delivery."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

from .config import ConfigError, load_config, load_dotenv, resolve_feishu_delivery
from .locking import RunLockedError
from .notifier import NotificationError
from .rule_test import _existing, _load_pending, approval_digest, send_test
from .zhihu_client import CollectorClient, CollectorError
from .zhihu_scan import (
    apply_reviews,
    create_scan,
    export_report,
    get_scan,
    resume_pending,
    summary,
)
from .zhihu_verify import preview, read_json, validate_article, write_json


def _state(database, run_id):
    state = get_scan(database, run_id)
    if not state:
        raise ConfigError("找不到知乎扫描任务")
    return state


def _semantic_review(path):
    raw = read_json(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("articles"), list):
        raise ConfigError("语境核对文件需要 articles 数组")
    entries = {}
    for item in raw["articles"]:
        if not isinstance(item, dict) or type(item.get("gpt6_is_model")) is not bool:
            raise ConfigError("每条核对必须明确 gpt6_is_model=true 或 false")
        key = item.get("article_key")
        if not isinstance(key, str) or not key or key in entries:
            raise ConfigError("语境核对缺少唯一文章键")
        if not isinstance(item.get("notes"), str) or not item["notes"].strip():
            raise ConfigError("语境核对需要 notes 说明实际模型语境")
        entries[key] = item
    return entries


def _validate_frozen(payload, scan_id, chat_id, limit_count, current_limit):
    """Check topic, factual review and scope without recreating a saved card."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ConfigError("发送快照格式无效")
    if not isinstance(chat_id, str) or not re.fullmatch(r"oc_[A-Za-z0-9]+", chat_id):
        raise ConfigError("固定发送范围缺少有效目标群")
    if type(limit_count) is not int or not 1 <= limit_count <= 5:
        raise ConfigError("固定发送范围超过本次最多五篇限制")
    result = payload["result"]
    article = validate_article(result["article"])
    key = article["article_key"]
    if payload["article_key"] != key or payload["chat_id"] != chat_id:
        raise ConfigError("发送快照的文章键或群与固定范围不一致")
    uuid.UUID(payload["send_uuid"])
    completeness = article.get("completeness")
    if (
        article["status"] != "body"
        or result.get("eligible") is not True
        or payload["rules"].get("fields") != ["body"]
        or not isinstance(completeness, dict)
        or not completeness.get("basis")
        or completeness.get("verified") is not True
        or completeness.get("target_id") != article["content_id"]
        or completeness.get("target_type") != article["content_type"]
        or completeness.get("target_id_verified") is not True
        or completeness.get("content_field_present") is not True
        or completeness.get("limitations") != []
    ):
        raise ConfigError("本次发送必须是有完整性依据且仅按正文筛选的合格内容")
    if (
        not re.search(
            r"(?<![A-Za-z0-9])gpt[\s\-‐‑–—]*6(?![A-Za-z0-9]|\.\d)",
            article["body"],
            re.IGNORECASE,
        )
        or "斩杀线" not in article["body"]
    ):
        raise ConfigError("正文缺少本次范围内的 GPT-6 或斩杀线")
    review = payload.get("semantic_review")
    if (
        not isinstance(review, dict)
        or review.get("article_key") != key
        or review.get("gpt6_is_model") is not True
        or review.get("body_sha256")
        != hashlib.sha256(article["body"].encode("utf-8")).hexdigest()
        or not isinstance(review.get("notes"), str)
        or not review["notes"].strip()
    ):
        raise ConfigError("发送快照缺少与完整正文绑定的模型语境核对")
    authorization = payload.get("authorization")
    if (
        not isinstance(authorization, dict)
        or authorization.get("scope") != "single_scan"
        or authorization.get("scan_id") != scan_id
        or type(authorization.get("max_results")) is not int
        or authorization["max_results"] != limit_count
    ):
        raise ConfigError("发送快照不属于此次扫描的固定发送范围")
    saved_limit = payload["max_payload_bytes"]
    if type(saved_limit) is not int or not 0 < saved_limit <= 30720:
        raise ConfigError("发送快照的卡片字节上限无效")
    if not isinstance(payload["card"], dict):
        raise ConfigError("发送快照缺少完整卡片")
    card_bytes = len(
        json.dumps(
            payload["card"], ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )
    if card_bytes > min(saved_limit, current_limit):
        raise NotificationError("固定卡片超过已保存或当前配置的字节上限")


def _delivery_record(database, payload):
    """Require an existing delivery to be exactly the campaign's frozen snapshot."""
    row = _existing(Path(database), payload["article_key"])
    if row is None:
        return None
    frozen = (
        _load_pending(row, payload["article_key"])
        if row["status"] == "pending"
        else json.loads(row["snapshot_json"])
    )
    digest = approval_digest(payload)
    if (
        approval_digest(frozen) != digest
        or row["approved_sha256"] != digest
        or row["article_key"] != payload["article_key"]
        or row["chat_id"] != payload["chat_id"]
        or row["send_uuid"] != payload["send_uuid"]
    ):
        raise ConfigError("既有发送记录与固定内容、规则、证据、群或 UUID 冲突")
    if row["status"] == "delivered" and (
        not row["message_id"] or row["delivered_chat_id"] != payload["chat_id"]
    ):
        raise ConfigError("已发送记录缺少消息 ID 或目标群不符")
    return row


def send_results(database, run_id, review_path, config_path):
    """Send at most five reviewed results of this one persisted scan."""
    state = _state(database, run_id)
    config = load_config(config_path)
    directory = Path(state["directory"])
    # Separate coordinator lock; send_test retains the shared Scout sender lock.
    with Path(str(database) + ".zhihu-delivery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with closing(sqlite3.connect(database)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS zhihu_send_campaigns (
                    scan_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
                    limit_count INTEGER NOT NULL CHECK(limit_count BETWEEN 1 AND 5),
                    authorized_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )""")
                conn.execute("""CREATE TABLE IF NOT EXISTS zhihu_campaign_articles (
                    scan_id TEXT NOT NULL, article_key TEXT NOT NULL,
                    position INTEGER NOT NULL, preview_json TEXT NOT NULL,
                    preview_sha256 TEXT NOT NULL,
                    PRIMARY KEY(scan_id,article_key), UNIQUE(scan_id,position)
                )""")
            campaign = conn.execute(
                "SELECT * FROM zhihu_send_campaigns WHERE scan_id=?", (state["id"],)
            ).fetchone()
            if campaign:
                rows = conn.execute(
                    "SELECT * FROM zhihu_campaign_articles WHERE scan_id=? ORDER BY position",
                    (state["id"],),
                ).fetchall()
                chat_id, limit_count = campaign["chat_id"], campaign["limit_count"]
                if len(rows) > limit_count or [row["position"] for row in rows] != list(
                    range(1, len(rows) + 1)
                ):
                    raise ConfigError("固定发送数量或发现顺序记录无效")
                payloads = []
                for row in rows:
                    payload = json.loads(row["preview_json"])
                    if (
                        approval_digest(payload) != row["preview_sha256"]
                        or row["article_key"] != payload["article_key"]
                    ):
                        raise ConfigError("固定发送快照的哈希或文章键校验失败")
                    payloads.append(payload)
            else:
                if review_path:
                    state = apply_reviews(
                        database, state["id"], _semantic_review(review_path)
                    )
                if state["status"] == "running":
                    print(
                        json.dumps(
                            {
                                "sent": 0,
                                "reason": "语境核对后继续在原范围内采集",
                                "collection": summary(state),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 2
                if state["status"] not in {"completed", "partial_failed"}:
                    raise ConfigError("扫描尚未结束；等待登录或采集失败不能当作无结果")
                delivery = resolve_feishu_delivery()
                if delivery.receive_id_type != "chat_id":
                    raise ConfigError("知乎结果必须发送到现有飞书群 chat_id")
                chat_id = delivery.receive_id
                limit_count = min(5, state["config"]["scan"]["max_results"])
                report = export_report(state)
                reviews = state.get("semantic_reviews", {})
                excluded = set(state.get("semantic_exclusions", []))
                selected = []
                skipped = []
                for result in report["results"]:
                    key = result["article"]["article_key"]
                    if not result["eligible"] or key in excluded:
                        continue
                    existing = _existing(Path(database), key)
                    if existing and existing["status"] == "delivered":
                        skipped.append(
                            {"article_key": key, "message_id": existing["message_id"]}
                        )
                        continue
                    selected.append((result, existing))
                    if len(selected) == limit_count:
                        break
                if not selected:
                    print(
                        json.dumps(
                            {
                                "sent": 0,
                                "reason": "符合条件的文章均已发送"
                                if skipped
                                else "没有符合条件的完整正文",
                                "skipped": skipped,
                                "collection": summary(state),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 0 if skipped else 2
                payloads = []
                for result, existing in selected:
                    article = result["article"]
                    key = article["article_key"]
                    if existing:
                        # Only the original scan may adopt its exact pending snapshot.
                        payload = _load_pending(existing, key)
                    else:
                        review = reviews.get(key)
                        if not review:
                            raise ConfigError(
                                f"缺少此正文的模型语境事实核对：{key}；使用 --review"
                            )
                        payload = preview(
                            directory,
                            "scores.json",
                            key,
                            chat_id,
                            config.feishu.max_payload_bytes,
                        )
                        payload["semantic_review"] = review
                        payload["authorization"] = {
                            "scope": "single_scan",
                            "scan_id": state["id"],
                            "max_results": limit_count,
                        }
                    payloads.append(payload)
            # Validate all frozen records before the first network send, including on recovery.
            for payload in payloads:
                _validate_frozen(
                    payload,
                    state["id"],
                    chat_id,
                    limit_count,
                    config.feishu.max_payload_bytes,
                )
                _delivery_record(database, payload)
            if not campaign:
                with conn:
                    conn.execute(
                        "INSERT INTO zhihu_send_campaigns(scan_id,chat_id,limit_count) VALUES (?,?,?)",
                        (state["id"], chat_id, limit_count),
                    )
                    for position, payload in enumerate(payloads, 1):
                        conn.execute(
                            "INSERT INTO zhihu_campaign_articles VALUES (?,?,?,?,?)",
                            (
                                state["id"],
                                payload["article_key"],
                                position,
                                json.dumps(
                                    payload, ensure_ascii=False, allow_nan=False
                                ),
                                approval_digest(payload),
                            ),
                        )
            sent = []
            for payload in payloads:
                key = payload["article_key"]
                existing = _delivery_record(database, payload)
                kwargs = {
                    "database_path": database,
                    "article_key": key,
                    "approval": approval_digest(payload),
                    "config": config,
                    "output": sys.stdout,
                }
                if existing:
                    # The persisted delivery is sufficient even if the run directory is gone.
                    send_test(preview_path=None, **kwargs)
                else:
                    # A campaign can be frozen before its individual delivery row exists.
                    # Use its embedded evidence; no collection file is required on recovery.
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".json", encoding="utf-8"
                    ) as stream:
                        json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                        stream.flush()
                        send_test(preview_path=Path(stream.name), **kwargs)
                row = _delivery_record(database, payload)
                sent.append(
                    {
                        "article_key": key,
                        "message_id": row["message_id"],
                        "status": row["status"],
                        "send_uuid": row["send_uuid"],
                    }
                )
            result = {"scan_id": state["id"], "deliveries": sent}
            try:
                directory.mkdir(parents=True, exist_ok=True)
                write_json(directory / "deliveries.json", result)
            except OSError:
                print(
                    "消息结果已保存 SQLite；运行目录不可写，未生成 deliveries.json。",
                    file=sys.stderr,
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"score", "preview", "send-test"}:
        from .zhihu_verify import legacy_main

        return legacy_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "查看采集器、登录与持久扫描状态"),
        ("scan", "采集并保存报告；不自动发送结果"),
        ("send-results", "发送本次授权的至多五篇结果；重试复用快照"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--database", type=Path)
        sub.add_argument("--run-id", help="Scout 扫描 UUID；默认最近一次")
        if name == "scan":
            sub.add_argument("--config", default="config.zhihu.toml")
            sub.add_argument("--request-uuid", help="相同 UUID 返回已保存的原扫描")
            sub.add_argument("--wait-seconds", type=float, default=60)
        if name == "send-results":
            sub.add_argument(
                "--review", type=Path, help="正文哈希与 GPT-6 模型语境核对 JSON"
            )
            sub.add_argument("--config", default="config.toml")
    commands.add_parser("score", help="对同一份正文按规则重新评分（详见 score --help）")
    commands.add_parser("preview", help="生成单篇卡片快照（详见 preview --help）")
    args = parser.parse_args(argv)
    try:
        load_dotenv()
        database = args.database or Path(
            os.environ.get("SCOUT_DB_PATH", "data/scout.sqlite3")
        )
        if args.command == "status":
            state = get_scan(database, args.run_id)
            client = CollectorClient.from_env()
            health = client.health()
            login = client.login_status()
            print(
                json.dumps(
                    {
                        "collector": health,
                        "login": {
                            key: login.get(key)
                            for key in ("status", "auth_method", "verified_at", "error")
                        },
                        "scan": summary(state) if state else None,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "send-results":
            return send_results(database, args.run_id, args.review, args.config)
        if args.wait_seconds < 0 or args.wait_seconds > 3600:
            raise ConfigError("wait-seconds 必须在 0..3600 内")
        state = (
            _state(database, args.run_id)
            if args.run_id
            else create_scan(database, args.config, args.request_uuid)
        )
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            resume_pending(database)
            state = _state(database, state["id"])
            if state["status"] != "running":
                break
            time.sleep(2)
        export_report(state)
        print(json.dumps(summary(state), ensure_ascii=False, indent=2))
        return 0 if state["status"] == "completed" else 2
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


if __name__ == "__main__":
    raise SystemExit(main())
