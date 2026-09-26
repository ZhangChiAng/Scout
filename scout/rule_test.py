"""Explicit rule test cards, independent of RSS and preferences."""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError, resolve_feishu_delivery
from .locking import sender_lock
from .notifier import FeishuNotifier, NotificationError


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def build_card(result, limit):
    article = result["article"]
    # Page text stays literal: no mentions or preference feedback actions.
    labels = {"title": "标题", "summary": "搜索摘要", "body": "正文"}
    reasons = []
    for hit in result["regex_hits"]:
        fields = "、".join(labels[field] for field in hit["fields"])
        reasons.append(f"正则：{hit['pattern']}\n命中字段：{fields}")
        for field, evidence in hit["evidence"].items():
            reasons.append(f"{labels[field]}命中片段：{evidence['text']}")
    for hit in result["keyword_hits"]:
        fields = "、".join(labels[field] for field in hit["fields"])
        reasons.append(f"关键词 {hit['text']}：{hit['weight']:+}（{fields}，只计一次）")
        for field, evidence in hit["evidence"].items():
            reasons.append(f"{labels[field]}关键词片段：{evidence['text']}")
    kind = "专栏文章" if article["content_type"] == "article" else "回答"
    prefix = (
        f"来源：{article['source']} · {kind}\n"
        f"{article['title']}\n作者：{article['author'] or '未知'}\n{article['url']}\n"
        f"得分：{result['score']} / 阈值：{result['threshold']}\n"
        + "\n".join(reasons)
        + "\n\n"
        + "正文摘录：\n"
    )
    body = article["body"]

    def card(size):
        excerpt = body[:size] + (
            "\n[摘录已截断，请查看原文]" if size < len(body) else ""
        )
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "知乎筛选结果"}},
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "plain_text", "content": prefix + excerpt},
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "type": "default",
                            "url": article["url"],
                            "text": {"tag": "plain_text", "content": "查看知乎原文"},
                        }
                    ],
                },
            ],
        }

    if len(_json(card(len(body))).encode()) <= limit:
        return card(len(body))
    low, high = 0, len(body)
    if len(_json(card(0)).encode()) > limit:
        raise NotificationError("result card metadata exceeds configured byte limit")
    while low < high:
        mid = (low + high + 1) // 2
        if len(_json(card(mid)).encode()) <= limit:
            low = mid
        else:
            high = mid - 1
    return card(low)


def approval_digest(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_preview(result, rules, evidence, card, chat_id, limit) -> dict:
    if not isinstance(chat_id, str) or not re.fullmatch(r"oc_[A-Za-z0-9]+", chat_id):
        raise ConfigError("测试预览需要具体飞书群 chat_id（oc_ 开头）")
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "article_key": result["article"]["article_key"],
        "result": result,
        "rules": rules,
        "evidence": evidence,
        "card": card,
        "chat_id": chat_id,
        "max_payload_bytes": limit,
        "send_uuid": str(uuid.uuid4()),
    }


def preview_display(payload: dict) -> dict:
    article = payload["result"]["article"]
    return {
        "article": {
            key: article[key]
            for key in ("article_key", "title", "url", "author", "status")
        },
        "rules": payload["rules"],
        "target_chat": payload["chat_id"],
        "card": payload["card"],
        "card_bytes": len(_json(payload["card"]).encode("utf-8")),
        "send_uuid": payload["send_uuid"],
        "approval_sha256": approval_digest(payload),
    }


def _existing(path: Path, article_key: str):
    if not path.exists():
        return None
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='rule_test_deliveries'"
        ).fetchone():
            return None
        return conn.execute(
            "SELECT * FROM rule_test_deliveries WHERE article_key=?", (article_key,)
        ).fetchone()


def _load_preview(path: Path, article_key: str) -> dict:
    from .rules import Rules
    from .zhihu_verify import evaluate_article, validate_article

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["schema_version"] != 1 or payload["article_key"] != article_key:
        raise ConfigError("预览版本或文章键不符")
    result = payload["result"]
    article = validate_article(result["article"])
    if article["article_key"] != article_key:
        raise ConfigError("预览中的内容 ID 与文章键不符")
    if not re.fullmatch(r"oc_[A-Za-z0-9]+", payload["chat_id"]):
        raise ConfigError("预览缺少有效的目标群")
    uuid.UUID(payload["send_uuid"])
    limit = payload["max_payload_bytes"]
    if type(limit) is not int or not 0 < limit <= 30720:
        raise ConfigError("预览卡片字节上限无效")
    rules = Rules.load(payload["rules"])
    if evaluate_article(article, rules) != result or not result["eligible"]:
        raise ConfigError("预览不是通过规则筛选的完整正文")
    if payload["card"] != build_card(result, limit):
        raise ConfigError("预览卡片与内容/评分不符，请重新生成预览")
    evidence = payload["evidence"]
    if {e["path"] for e in evidence} != set(article["raw_files"]):
        raise ConfigError("预览缺少原始工具返回快照")
    for entry in evidence:
        if (
            hashlib.sha256(entry["content"].encode("utf-8")).hexdigest()
            != entry["sha256"]
        ):
            raise ConfigError("预览证据校验失败")
    return payload


def _load_pending(row, article_key: str) -> dict:
    """Validate the saved delivery without rebuilding it from current inputs."""
    try:
        payload = json.loads(row["snapshot_json"])
        if (
            not isinstance(payload, dict)
            or payload["schema_version"] != 1
            or approval_digest(payload) != row["approved_sha256"]
        ):
            raise ConfigError("待发送快照校验失败")
        if (
            row["status"] != "pending"
            or row["article_key"] != article_key
            or payload["article_key"] != article_key
            or payload["result"]["article"]["article_key"] != article_key
            or payload["chat_id"] != row["chat_id"]
            or payload["send_uuid"] != row["send_uuid"]
        ):
            raise ConfigError("待发送快照与发送记录的文章、群或 UUID 不一致")
        if not isinstance(payload["chat_id"], str) or not re.fullmatch(
            r"oc_[A-Za-z0-9]+", payload["chat_id"]
        ):
            raise ConfigError("待发送快照的目标群无效")
        if not isinstance(payload["send_uuid"], str):
            raise ConfigError("待发送快照的 UUID 无效")
        uuid.UUID(payload["send_uuid"])
        limit = payload["max_payload_bytes"]
        if type(limit) is not int or not 0 < limit <= 30720:
            raise ConfigError("待发送快照的卡片字节上限无效")
        if not isinstance(payload["card"], dict):
            raise ConfigError("待发送快照缺少完整卡片")
        if len(_json(payload["card"]).encode("utf-8")) > limit:
            raise NotificationError("saved card exceeds its approved byte limit")
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("待发送快照格式无效") from exc
    return payload


def send_test(*, database_path, article_key, preview_path, approval, config, output):
    path = Path(database_path)
    with sender_lock(path):
        row = _existing(path, article_key)
        if row and row["status"] == "delivered":
            print("已发送，跳过：" + row["message_id"], file=output)
            return 0
        if row:
            # Recovery must not depend on files, current rules, or a fresh search.
            payload = _load_pending(row, article_key)
        else:
            if preview_path is None:
                raise ConfigError("首次发送需要 --preview；已有待发送记录仅需文章键")
            payload = _load_preview(preview_path, article_key)
        if (
            len(_json(payload["card"]).encode("utf-8"))
            > config.feishu.max_payload_bytes
        ):
            raise NotificationError("saved card exceeds configured byte limit")
        print(
            json.dumps(preview_display(payload), ensure_ascii=False, indent=2),
            file=output,
            flush=True,
        )
        digest = approval_digest(payload)
        if approval is None:
            expected = f"SEND {article_key} {payload['chat_id']}"
            try:
                confirmed = input(f"授权发送以上文章与卡片，请输入 {expected}：")
            except EOFError:
                confirmed = ""
            if confirmed != expected:
                print("未授权，未发送。", file=output)
                return 1
        elif approval != digest:
            raise ConfigError("--approve 与具体预览摘要不一致，未发送")
        delivery = replace(
            resolve_feishu_delivery(),
            receive_id_type="chat_id",
            receive_id=payload["chat_id"],
        )
        with closing(sqlite3.connect(path)) as conn:
            with conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS rule_test_deliveries (
                    article_key TEXT PRIMARY KEY,
                    snapshot_json TEXT NOT NULL,
                    approved_sha256 TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    send_uuid TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('pending','delivered')),
                    message_id TEXT,
                    delivered_chat_id TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    approved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    delivered_at TEXT
                )""")
                if row is None:
                    conn.execute(
                        """INSERT INTO rule_test_deliveries
                        (article_key,snapshot_json,approved_sha256,chat_id,send_uuid,status)
                        VALUES (?,?,?,?,?,'pending')""",
                        (
                            article_key,
                            _json(payload),
                            digest,
                            payload["chat_id"],
                            payload["send_uuid"],
                        ),
                    )
            try:
                sent = FeishuNotifier(
                    delivery, config.network.timeout_seconds
                ).send_card(
                    payload["card"],
                    send_uuid=payload["send_uuid"],
                    chat_id=payload["chat_id"],
                )
            except NotificationError as exc:
                with conn:
                    conn.execute(
                        "UPDATE rule_test_deliveries SET last_error=? WHERE article_key=?",
                        (str(exc), article_key),
                    )
                raise
            with conn:
                conn.execute(
                    """UPDATE rule_test_deliveries SET status='delivered',
                    message_id=?, delivered_chat_id=?, last_error='',
                    delivered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE article_key=?""",
                    (sent.message_id, sent.chat_id, article_key),
                )
        print("已发送：" + sent.message_id, file=output)
        return 0
