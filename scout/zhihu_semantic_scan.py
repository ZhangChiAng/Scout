"""Resumable, owner-driven Zhihu search, semantic evaluation and delivery.

The scan JSON is the durable journal: every external request has a saved UUID,
every batch freezes its ordered identities and preference, and callbacks only
append control jobs. No SQLite transaction spans collector/model/Feishu I/O.
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
import uuid
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import zhihu_delivery, zhihu_learning, zhihu_store
from .codex_runtime import CodexBusyError, LLMError
from .config import ConfigError, load_config, resolve_feishu_delivery
from .database import connect, transaction
from .datetime_utils import BEIJING_TIMEZONE
from .locking import sender_lock
from .storage import FeedbackError
from .zhihu_client import CollectorClient, CollectorError
from .zhihu_query_plan import (
    EVALUATION_POLICY,
    install_query_plan,
)
from .zhihu_verify import complete_body

DEFAULT_PARAMS = {
    "search_latest_pages_per_advance": 1,
    "search_general_pages_per_advance": 1,
    "question_pages_per_advance": 1,
    "evaluation_batch_size": 8,
    "model_request_size": 1,
}


def parameters():
    path = Path(os.environ.get("SCOUT_ZHIHU_PARAMS", "config.zhihu-semantic.toml"))
    supplied = tomllib.loads(path.read_text()) if path.exists() else {}
    values = {**DEFAULT_PARAMS, **supplied.get("semantic", {})}
    if set(values) != set(DEFAULT_PARAMS) or any(
        type(v) is not int or not 1 <= v <= 100 for v in values.values()
    ):
        raise ConfigError("知乎试用参数必须是已知名称及 1–100 的整数")
    if values["model_request_size"] != 1:
        raise ConfigError("模型请求必须逐篇执行")
    if values["evaluation_batch_size"] > 32:
        raise ConfigError("每批最多处理 32 篇候选")
    return values


def _now():
    return datetime.now(UTC).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def create_scan(database, topic_id, request_uuid=None):
    scan_id = str(uuid.UUID(request_uuid)) if request_uuid else str(uuid.uuid4())
    with sender_lock(database):
        zhihu_delivery.initialize(database)
        zhihu_store.initialize(database)
    with closing(connect(database)) as conn:
        row = conn.execute(
            "SELECT state_json FROM zhihu_scans WHERE id=?", (scan_id,)
        ).fetchone()
        if row:
            previous = json.loads(row[0])
            if previous.get("topic", {}).get("id") != topic_id:
                raise ConfigError("同一扫描 UUID 不能用于不同话题")
            return previous
    topic = zhihu_store.get_topic(database, topic_id)
    if not topic or not topic["enabled"]:
        raise ConfigError("话题不存在或已停用")
    delivery = resolve_feishu_delivery()
    if delivery.receive_id_type != "chat_id":
        raise ConfigError("知乎卡片必须发送到配置的飞书群")
    now = datetime.now(UTC)
    directory = Path(database).resolve().parent / "zhihu-runs" / scan_id
    (directory / "raw").mkdir(parents=True, exist_ok=True, mode=0o700)
    state = {
        "schema_version": 3,
        "evaluation_policy": EVALUATION_POLICY,
        "experiment_round": 1,
        "id": scan_id,
        "created_at": now.isoformat(),
        "window_end": now.isoformat(),
        "window_start": (now - timedelta(days=30)).isoformat(),
        "topic": topic,
        "params": parameters(),
        "query_plan": [],
        "query_plan_frozen": False,
        "query_contributions": {},
        "queries": [],
        "questions": {},
        "candidates": {},
        "batches": [],
        "active": None,
        "directory": str(directory),
        "chat_id": delivery.receive_id,
        "status": "running",
        "phase": "rewrite",
        "stop_reason": "",
        "last_error": None,
        "request": {"action": "first", "quantity": None, "qualified": 0},
        "controls": [],
        "operations": [],
        "metrics": {},
        "errors": [],
        "cycle": 0,
        "collection": None,
        "stop_requested": False,
    }
    with transaction(database) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO zhihu_scans VALUES (?,?,?,?)",
            (scan_id, state["created_at"], state["status"], _json(state)),
        )
    return state


def _resolve_scan(conn, scan_id):
    row = conn.execute(
        "SELECT state_json FROM zhihu_scans WHERE id=?", (scan_id,)
    ).fetchone()
    if row is None:
        alias = conn.execute(
            "SELECT value_json FROM zhihu_settings WHERE key=?",
            ("scan_alias:" + scan_id,),
        ).fetchone()
        if alias:
            row = conn.execute(
                "SELECT state_json FROM zhihu_scans WHERE id=?", (json.loads(alias[0]),)
            ).fetchone()
    return json.loads(row[0]) if row else None


def request_control(
    database,
    scan_id,
    action,
    quantity=None,
    event_id="",
    *,
    parameter_overrides=None,
    advance_collection=False,
):
    """Callback-safe command persistence; stop is visible even during model I/O."""
    scan_id = str(uuid.UUID(scan_id))
    if action not in {"continue", "append", "stop"}:
        raise FeedbackError("未知扫描操作")
    if action == "append" and (type(quantity) is not int or not 1 <= quantity <= 10000):
        raise FeedbackError("追加数量需为 1–10000 的整数")
    overrides = parameter_overrides or {}
    adjustable = {
        "evaluation_batch_size",
        "search_latest_pages_per_advance",
        "search_general_pages_per_advance",
        "question_pages_per_advance",
    }
    if (
        not isinstance(overrides, dict)
        or len(overrides) > 1
        or set(overrides) - adjustable
        or any(type(v) is not int or not 1 <= v <= 100 for v in overrides.values())
        or overrides.get("evaluation_batch_size", 1) > 32
        or type(advance_collection) is not bool
        or (action == "stop" and (overrides or advance_collection))
    ):
        raise FeedbackError("每次只调整一个参数；页数为 1–100，每批候选数为 1–32")
    payload = {
        "scan_id": scan_id,
        "action": action,
        "quantity": quantity,
        "parameter_overrides": overrides,
        "advance_collection": advance_collection,
    }
    with transaction(database, rows=True, timeout=0.3) as conn:
        current = _resolve_scan(conn, scan_id)
        if not current or current.get("schema_version") != 3:
            raise FeedbackError("此操作仅用于语义扫描轮次")
        scan_id = current["id"]
        payload["scan_id"] = scan_id
        if event_id:
            old = conn.execute(
                "SELECT * FROM zhihu_jobs WHERE event_id=?", (event_id,)
            ).fetchone()
            if old:
                old_payload = json.loads(old["payload_json"])
                old_target = _resolve_scan(conn, old_payload.get("scan_id"))
                if (
                    old["kind"] != "semantic_control"
                    or any(
                        old_payload.get(k) != v
                        for k, v in payload.items()
                        if k != "scan_id"
                    )
                    or not old_target
                    or old_target["id"] != scan_id
                ):
                    raise FeedbackError("同一事件不能提交不同操作")
                return {"id": old["id"], "status": old["status"], "scan_id": scan_id}
        if (
            current.get("evaluation_policy") == EVALUATION_POLICY
            and (overrides or advance_collection)
            and (
                current.get("active")
                or current.get("collection")
                or (
                    current["batches"]
                    and current["batches"][-1]["status"] != "completed"
                )
            )
        ):
            raise FeedbackError("当前批次尚未完成，恢复本批后再调整下一批参数")
        stamp = _now()
        if action != "stop":
            feedback_revision = conn.execute(
                "SELECT coalesce(max(revision_id),0) FROM zhihu_feedback_revisions"
            ).fetchone()[0]
            ready = conn.execute(
                "SELECT cutoff_revision_id FROM zhihu_semantic_preferences "
                "WHERE status='ready' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            trained_revision = ready[0] if ready else 0
            payload.update(
                feedback_revision_at_request=feedback_revision,
                trained_revision_at_request=trained_revision,
                preference_ready_at_request=feedback_revision <= trained_revision,
            )
        payload["baseline_keys"] = [
            r[0]
            for r in conn.execute(
                "SELECT article_key FROM zhihu_link_deliveries WHERE scan_id=?",
                (scan_id,),
            )
        ]
        cursor = conn.execute(
            "INSERT INTO zhihu_jobs(kind,payload_json,event_id,created_at,updated_at) VALUES ('semantic_control',?,?,?,?)",
            (_json(payload), event_id or None, stamp, stamp),
        )
        if action == "stop":
            conn.execute(
                "INSERT OR REPLACE INTO zhihu_settings VALUES (?,?)",
                ("semantic_stop:" + scan_id, "true"),
            )
        return {"id": cursor.lastrowid, "status": "pending", "scan_id": scan_id}


def stop_requested(database, scan_id):
    with closing(connect(database, read_only=True)) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM zhihu_settings WHERE key=?",
                ("semantic_stop:" + scan_id,),
            ).fetchone()
            is not None
        )


def _save(database, state):
    # An operation started before a stop may finish; its result is saved before
    # stopping. Existing queued sends are held by the delivery worker as well.
    if stop_requested(database, state["id"]):
        state["stop_requested"] = True
        state["status"] = "stopping" if state.get("active") else "stopped"
        state["stop_reason"] = "user_stop"
    with transaction(database) as conn:
        conn.execute(
            "UPDATE zhihu_scans SET status=?,state_json=? WHERE id=?",
            (state["status"], _json(state), state["id"]),
        )


def _controls(database, state):
    with transaction(database, rows=True) as conn:
        jobs = conn.execute(
            "SELECT * FROM zhihu_jobs WHERE kind='semantic_control' AND status='pending' AND json_extract(payload_json,'$.scan_id')=? ORDER BY id",
            (state["id"],),
        ).fetchall()
        for job in jobs:
            command = json.loads(job["payload_json"])
            action = command["action"]
            state["controls"].append(
                {**command, "job_id": job["id"], "at": job["created_at"]}
            )
            if action == "stop":
                conn.execute(
                    "INSERT OR REPLACE INTO zhihu_settings VALUES (?,?)",
                    ("semantic_stop:" + state["id"], "true"),
                )
                state.update(
                    stop_requested=True,
                    status="stopping" if state["active"] else "stopped",
                    stop_reason="user_stop",
                )
            elif state["status"] in {
                "waiting_user",
                "stopped",
                "stopping",
                "exhausted",
                "collector_failed",
                "model_failed",
                "model_busy",
                "waiting_login",
                "delivery_failed",
                "processing_failed",
                "waiting_preference",
            }:
                conn.execute(
                    "DELETE FROM zhihu_settings WHERE key=?",
                    ("semantic_stop:" + state["id"],),
                )
                state.update(
                    stop_requested=False,
                    status="running",
                    stop_reason="",
                    last_error=None,
                )
                previous_request = state["request"]
                continuing_append = (
                    action == "continue"
                    and previous_request.get("action") == "append"
                    and previous_request.get("qualified", 0)
                    < previous_request["quantity"]
                )
                if not continuing_append:
                    state["request"] = {
                        "action": action,
                        "quantity": command["quantity"],
                        "qualified": 0,
                        "baseline_keys": command.get("baseline_keys", []),
                    }
                overrides = command.get("parameter_overrides", {})
                if overrides:
                    state["controls"][-1]["parameters_before"] = dict(state["params"])
                    state["params"].update(overrides)
                    state["controls"][-1]["parameters_after"] = dict(state["params"])
                if command.get("advance_collection"):
                    state["force_collection"] = True
                # Reuse an existing batch and all successful results on retry.
                if state["batches"] and state["batches"][-1]["status"] != "completed":
                    for key in state["batches"][-1]["keys"]:
                        candidate = state["candidates"][key]
                        if candidate["status"] in {
                            "body_failed",
                            "model_failed",
                            "relevance_failed",
                            "evaluation_failed",
                        }:
                            body_failed = candidate["status"] == "body_failed"
                            candidate["status"] = "pending"
                            candidate.pop("error", None)
                            if body_failed:
                                candidate.pop("body_attempted", None)
                                candidate["body_retries"] = (
                                    candidate.get("body_retries", 0) + 1
                                )
                            state["batches"][-1]["results"].pop(key, None)
                else:
                    for candidate in state["candidates"].values():
                        if candidate["status"] in {
                            "body_failed",
                            "model_failed",
                            "relevance_failed",
                            "evaluation_failed",
                        }:
                            body_failed = candidate["status"] == "body_failed"
                            candidate["status"] = "pending"
                            candidate.pop("error", None)
                            if body_failed:
                                candidate.pop("body_attempted", None)
                                candidate["body_retries"] = (
                                    candidate.get("body_retries", 0) + 1
                                )
                active = state.get("active")
                if active and active.get("terminal_failure"):
                    if active["payload"]["kind"] in {
                        "search_page",
                        "question_answers_page",
                    }:
                        active["resume_requested"] = True
                    else:
                        active["attempt"] = active.get("attempt", 0) + 1
                        active["payload"]["request_uuid"] = str(
                            uuid.uuid5(
                                uuid.UUID(state["id"]),
                                active["label"] + f":retry:{active['attempt']}",
                            )
                        )
                        active["collector_id"] = None
                    active.pop("terminal_failure", None)
                conn.execute(
                    "UPDATE zhihu_link_deliveries SET status='pending',attempts=0,retry_at=0,last_error='' WHERE scan_id=? AND (status='failed' OR (status='sending' AND attempts>=3))",
                    (state["id"],),
                )
                if command.get("preference_ready_at_request") is False and (
                    not state["batches"]
                    or state["batches"][-1]["status"] == "completed"
                ):
                    # The worker may finish training before consuming this
                    # command. That earlier click cannot authorize a new batch
                    # after a preference update the owner had not yet seen.
                    state.update(
                        status="waiting_preference",
                        stop_reason="feedback_update_pending",
                    )
            else:
                state["controls"][-1]["ignored"] = (
                    "当前请求尚在处理，本操作未重复启动批次"
                )
            conn.execute(
                "UPDATE zhihu_jobs SET status='completed',updated_at=? WHERE id=?",
                (_now(), job["id"]),
            )
        if jobs:
            conn.execute(
                "UPDATE zhihu_scans SET status=?,state_json=? WHERE id=?",
                (state["status"], _json(state), state["id"]),
            )


def _metric(state, name, value=1):
    metrics = state["metrics"]
    metrics[name] = metrics.get(name, 0) + value


def _history(database):
    with closing(connect(database, read_only=True)) as conn:
        keys = dict(
            conn.execute("SELECT article_key,status FROM zhihu_link_deliveries")
        )
        keys.update(
            dict(
                conn.execute(
                    "SELECT s.content_key,d.status FROM zhihu_card_deliveries d JOIN zhihu_content_snapshots s ON s.snapshot_id=d.snapshot_id"
                )
            )
        )
    return keys


def _basic_reason(state, article, history):
    if article["article_key"] in history:
        return (
            "historical_delivered"
            if history[article["article_key"]] == "delivered"
            else "delivery_reserved"
        )
    value = article.get("published_at", "")
    if not value:
        return "date_missing"
    stamp = _published(value)
    if stamp is None:
        return "date_invalid"
    if stamp > datetime.fromisoformat(state["window_end"]):
        return "date_future"
    if stamp < datetime.fromisoformat(state["window_start"]):
        return "date_expired"
    return None


def _question(state, article, source):
    qid = article.get("question_id")
    if not qid or article["content_type"] != "answer":
        return
    question = state["questions"].setdefault(
        qid,
        {
            "question_id": qid,
            "cursor": None,
            "is_end": False,
            "pages": 0,
            "rank_count": 0,
            "sources": [],
        },
    )
    if source not in question["sources"]:
        question["sources"].append(source)


def _merge(database, state, article, source):
    key = article["article_key"]
    votes = article.get("voteup_count")
    article["voteup_count"] = votes if type(votes) is int and votes >= 0 else None
    existing = state["candidates"].get(key)
    if existing is None:
        existing = {
            "article": article,
            "sources": [],
            "status": "pending",
            "discovered_cycle": state["cycle"],
        }
        state["candidates"][key] = existing
        _metric(state, "new_candidates")
    else:
        _metric(state, "duplicates")
        old = existing["article"]
        combined = {
            **old,
            **{k: v for k, v in article.items() if v is not None and v != ""},
        }
        if complete_body(old) and not complete_body(article):
            for field in (
                "body",
                "status",
                "completeness",
                "raw_files",
                "evidence",
                "read_error",
            ):
                combined[field] = old.get(field)
        existing["article"] = combined
        if existing["status"] in {
            "date_missing",
            "date_invalid",
            "date_expired",
            "date_future",
        }:
            existing["status"] = "pending"
    if source not in existing["sources"]:
        existing["sources"].append(source)
    zhihu_store.save_discovery(
        database,
        state["id"],
        state["topic"]["id"],
        key,
        source["source"],
        search_term=source.get("query", ""),
        question_id=article.get("question_id", ""),
        rank=source.get("list_rank"),
        details=source,
    )
    if source["source"] in {"search_page", "seed_detail"}:
        _question(state, existing["article"], source)
    return existing


def _pending(database, state):
    history = _history(database)
    result = []
    for key, candidate in state["candidates"].items():
        if candidate["status"] != "pending":
            continue
        article = candidate["article"]
        reason = _basic_reason(state, article, history)
        if reason:
            candidate.update(status=reason, reason=reason)
            _metric(state, reason)
            zhihu_store.save_candidate_result(
                database, state["id"], state["topic"]["id"], article, reason=reason
            )
            continue
        result.append(key)

    def order(key):
        article = state["candidates"][key]["article"]
        votes = article.get("voteup_count")
        return (
            votes is None,
            -(votes or 0),
            -_published(article["published_at"]).timestamp(),
            key,
        )

    return sorted(result, key=order)


def _uncovered(state):
    return any(not row["is_end"] for row in state["queries"]) or any(
        not row["is_end"] for row in state["questions"].values()
    )


def _start_operation(database, state, label, payload, **metadata):
    if stop_requested(database, state["id"]):
        return
    state["active"] = {
        "label": label,
        "payload": {
            **payload,
            "request_uuid": str(uuid.uuid5(uuid.UUID(state["id"]), label)),
        },
        "collector_id": None,
        "started_at": _now(),
        **metadata,
    }
    _save(database, state)


def _advance(database, state):
    if state["collection"] is None:
        state["cycle"] += 1
        state["collection"] = {
            "search_targets": [
                q["pages"] + state["params"][f"search_{q['sort']}_pages_per_advance"]
                for q in state["queries"]
            ],
            "question_targets": None,
            "started_at": _now(),
        }
    collection = state["collection"]
    for index, query in enumerate(state["queries"]):
        if not query["is_end"] and query["pages"] < collection["search_targets"][index]:
            _start_operation(
                database,
                state,
                f"search:{index}:{query['pages']}",
                {
                    "kind": "search_page",
                    "query": query["query"],
                    "sort": query["sort"],
                    "cursor": query["cursor"],
                },
                stream_index=index,
            )
            return False
    for key, candidate in state["candidates"].items():
        article = candidate["article"]
        if (
            article["content_type"] == "answer"
            and not article.get("question_id")
            and not candidate.get("seed_resolved")
        ):
            _start_operation(
                database,
                state,
                "resolve:" + key,
                {"kind": "detail", "items": [article]},
                purpose="seed_detail",
                content_key=key,
            )
            return False
    if collection["question_targets"] is None:
        collection["question_targets"] = {
            qid: q["pages"] + state["params"]["question_pages_per_advance"]
            for qid, q in state["questions"].items()
        }
    for qid, target in collection["question_targets"].items():
        question = state["questions"][qid]
        if not question["is_end"] and question["pages"] < target:
            _start_operation(
                database,
                state,
                f"question:{qid}:{question['pages']}",
                {
                    "kind": "question_answers_page",
                    "question_id": qid,
                    "sort": "default",
                    "cursor": question["cursor"],
                },
            )
            return False
    _metric(
        state,
        "collection_seconds",
        (
            datetime.now(UTC) - datetime.fromisoformat(collection["started_at"])
        ).total_seconds(),
    )
    state["collection"] = None
    return True


def _poll(database, state, client):
    from .zhihu_scan import _record

    active = state["active"]
    if active.get("resume_requested") and not stop_requested(database, state["id"]):
        client.resume(active["collector_id"])
        active.pop("resume_requested")
        _save(database, state)
    if active["collector_id"] is None:
        if stop_requested(database, state["id"]):
            state["active"] = None  # no upstream request has started
            return
        remote = client.start(active["payload"])
        active["collector_id"] = remote["id"]
        _save(database, state)
    result = client.run(active["collector_id"])
    status = result["status"]
    if status in {"pending", "running", "queued"}:
        return
    if status == "waiting_login":
        raise CollectorError("login", "知乎会话失效，请完成采集器登录后继续")
    kind = active["payload"]["kind"]
    if status != "completed" and not (kind == "detail" and status == "partial_failed"):
        active["terminal_failure"] = True
        raise CollectorError("collection", "采集任务失败，已有候选及分页位置保留")
    response = client.records(active["collector_id"])
    records = response.get("records")
    if not isinstance(records, list):
        raise CollectorError("protocol", "采集任务缺少内容记录")
    stream = None
    if kind in {"search_page", "question_answers_page"}:
        page = response.get("page", result.get("page"))
        if not isinstance(page, dict) or type(page.get("is_end")) is not bool:
            raise CollectorError("protocol", "分页任务缺少真实结束标志")
        if not page["is_end"] and not page.get("next_cursor"):
            raise CollectorError("protocol", "未结束分页缺少后续游标")
        stream = (
            state["queries"][active["stream_index"]]
            if kind == "search_page"
            else state["questions"][active["payload"]["question_id"]]
        )
        if page.get("next_cursor") and page["next_cursor"] == stream["cursor"]:
            raise CollectorError("protocol", "采集器分页没有推进")
    for index, raw in enumerate(records):
        article = _record(client, state, raw)
        source = {
            "source": active.get("purpose", kind),
            "operation": active["label"],
            "collector_id": active["collector_id"],
            "collected_at": article["fetched_at"],
        }
        if kind == "search_page":
            contributions = active.setdefault("search_contributions", {})
            contribution = contributions.setdefault(
                str(index),
                "duplicate" if article["article_key"] in state["candidates"] else "new",
            )
            source.update(
                query=stream["query"],
                sort=stream["sort"],
                page=stream["pages"] + 1,
                contribution=contribution,
            )
        elif kind == "question_answers_page":
            source.update(
                question_id=stream["question_id"],
                list_rank=raw.get("list_rank") or stream["rank_count"] + index + 1,
                search_sources=stream["sources"],
                page=stream["pages"] + 1,
            )
        else:
            if article["article_key"] != active["content_key"]:
                raise CollectorError("protocol", "详情身份与请求不一致")
        candidate = _merge(database, state, article, source)
        if active.get("purpose") == "seed_detail":
            candidate["seed_resolved"] = True
            if not candidate["article"].get("question_id"):
                candidate["seed_error"] = "详情缺少所属问题 ID，保留原搜索命中"
                _metric(state, "question_id_missing")
        elif kind == "detail":
            candidate["body_attempted"] = True
    if kind == "detail" and not records:
        candidate = state["candidates"][active["content_key"]]
        if active.get("purpose") == "seed_detail":
            candidate.update(seed_resolved=True, seed_error="详情未返回所属问题")
        else:
            candidate["body_attempted"] = True
    if stream is not None:
        stream.update(
            cursor=page.get("next_cursor"),
            is_end=page["is_end"],
            pages=stream["pages"] + 1,
        )
        if kind == "question_answers_page":
            stream["rank_count"] += int(page.get("raw_count", len(records)))
            _metric(state, "question_pages")
            _metric(state, "expanded_questions", int(stream["pages"] == 1))
            zhihu_store.save_expansion(
                database,
                state["id"],
                stream["question_id"],
                str(
                    uuid.uuid5(
                        uuid.UUID(state["id"]), "question:" + stream["question_id"]
                    )
                ),
                status="exhausted" if stream["is_end"] else "available",
                progress=stream,
            )
        else:
            _metric(state, "search_pages")
            totals = state["query_contributions"][stream["query"]]
            counts = Counter(active.get("search_contributions", {}).values())
            for counter, increment in (
                ("new_candidates", counts["new"]),
                ("duplicates", counts["duplicate"]),
                ("records", len(records)),
            ):
                stream[counter] = stream.get(counter, 0) + increment
                totals[counter] += increment
            totals["pages"] += 1
    state["operations"].append(
        {
            **active,
            "completed_at": _now(),
            "records": len(records),
            "page": response.get("page", result.get("page")),
            "coverage": response.get("coverage", []),
        }
    )
    _metric(
        state,
        kind + "_seconds",
        (
            datetime.now(UTC) - datetime.fromisoformat(active["started_at"])
        ).total_seconds(),
    )
    state["active"] = None


def _batch_preference(database, state):
    preference = zhihu_learning.snapshot(database)
    previous = state["batches"][-1] if state["batches"] else None
    if (
        previous
        and previous["status"] == "completed"
        and any(result.get("decision") for result in previous["results"].values())
        and preference["feedback_revision"] <= previous["feedback_revision"]
    ):
        state.update(
            status="waiting_user", stop_reason="feedback_required", learning=preference
        )
        _save(database, state)
        _notify(database, state, previous, "feedback_required")
        return None
    if preference["training_pending"]:
        state.update(
            status="waiting_preference",
            stop_reason="feedback_update_pending",
            learning=preference,
            last_error={
                "kind": "preference_update",
                "message": preference["training_error"],
            }
            if preference["training_error"]
            else None,
        )
        _save(database, state)
        if state["batches"]:
            _notify(
                database,
                state,
                state["batches"][-1],
                f"preference:{preference['feedback_revision']}:{preference['training_status']}",
            )
        return None
    return preference


def _new_batch(database, state, keys):
    preference = _batch_preference(database, state)
    if preference is None:
        return None
    number = len(state["batches"]) + 1
    batch = {
        "id": str(uuid.uuid5(uuid.UUID(state["id"]), f"batch:{number}")),
        "number": number,
        "keys": keys[: min(32, state["params"]["evaluation_batch_size"])],
        "preference": preference,
        "feedback_revision": preference["feedback_revision"],
        "params": dict(state["params"]),
        "results": {},
        "status": "evaluating",
        "started_at": _now(),
        "stats": {},
        "metrics_start": dict(state.get("last_batch_metrics", {})),
        "metrics": {},
    }
    state["batches"].append(batch)
    _save(database, state)
    return batch


def _save_result(database, state, batch, key, result):
    candidate = state["candidates"][key]
    candidate.update(
        status=result.get("decision", result.get("error", "model_failed")),
        result=result,
    )
    article = candidate["article"]
    for field in ("summary", "semantic_result", "decision", "decision_reason"):
        article.pop(field, None)
    stages = candidate.get("stages", {})
    article.update(
        semantic=result,
        stages=stages,
        evaluation_policy="relevance_v2",
        semantic_batch_id=batch["id"],
        semantic_preference_version=batch["preference"].get("version"),
    )
    for stage in ("relevance", "evaluation"):
        saved = stages.get(stage, {})
        if saved.get("status") == "completed":
            article[stage] = saved["result"]
        else:
            article.pop(stage, None)
    # A body-only or old-policy snapshot must never be reused as this judgment.
    # The hash makes retrying this exact completed result idempotent.
    if complete_body(article):
        article.pop("snapshot_id", None)
        article["snapshot_id"] = zhihu_store.save_content(
            database, article, scan_id=state["id"]
        )["snapshot_id"]
        candidate["snapshot_id"] = article["snapshot_id"]
    zhihu_store.save_candidate_result(
        database,
        state["id"],
        state["topic"]["id"],
        article,
        snapshot_id=candidate.get("snapshot_id"),
        reason=candidate["status"],
        model_version=batch["preference"].get("version"),
    )
    batch["results"][key] = result
    _save(database, state)


def _run_model_stage(database, state, batch, key, stage_name):
    from .zhihu_semantics import classify_relevance, evaluate_content

    candidate = state["candidates"][key]
    stage = candidate.setdefault("stages", {}).setdefault(stage_name, {"attempts": []})
    # A process interruption has unknown remote completion; preserve the attempt
    # record. A completed local stage always returns through _evaluate's cache.
    for old in stage["attempts"]:
        if old["status"] == "running":
            old.update(status="interrupted", error="进程中断，未取得可复用结果")
    attempt = {
        "batch_id": batch["id"],
        "status": "running",
        "started_at": _now(),
        "telemetry": {},
    }
    stage["attempts"].append(attempt)
    stage.update(status="running", batch_id=batch["id"])
    _save(database, state)
    started = time.monotonic()
    result, failure = None, None
    try:
        if stage_name == "relevance":
            result = classify_relevance(
                state["topic"], candidate["article"], telemetry=attempt["telemetry"]
            )
        else:
            result = evaluate_content(
                state["topic"],
                batch["preference"],
                candidate["article"],
                relevance=candidate["stages"]["relevance"]["result"],
                telemetry=attempt["telemetry"],
            )
    except (LLMError, ConfigError, ValueError, TypeError, OSError) as exc:
        failure = exc
    finally:
        telemetry = attempt["telemetry"]
        telemetry.setdefault("duration_seconds", round(time.monotonic() - started, 3))
        attempt["completed_at"] = _now()
        _metric(state, "model_calls", telemetry.get("calls", 0))
        _metric(state, stage_name + "_model_calls", telemetry.get("calls", 0))
        _metric(state, stage_name + "_seconds", telemetry["duration_seconds"])
        stage["telemetry"] = dict(telemetry)
    if failure is not None:
        error = str(failure)[:300]
        attempt.update(status="failed", error=error)
        stage.update(status="failed", error=error)
        kind = stage_name + "_failed"
        state["errors"].append(
            {
                "kind": kind,
                "stage": stage_name,
                "content_key": key,
                "batch_id": batch["id"],
                "message": error,
                "at": _now(),
            }
        )
        _metric(state, kind)
        state.update(
            status="model_busy"
            if isinstance(failure, CodexBusyError)
            else "model_failed",
            last_error={"kind": kind, "stage": stage_name, "message": error},
        )
        _save_result(
            database,
            state,
            batch,
            key,
            {"error": kind, "stage": stage_name, "reason": error},
        )
        _save(database, state)
        return None
    attempt["status"] = "completed"
    stage.update(status="completed", result=result, completed_at=_now())
    stage.pop("error", None)
    # Persist this success before preparing snapshots, sending, or calling the
    # other model. Recovery therefore reuses Luna even after a Sol failure.
    _save(database, state)
    return result


def _evaluate(database, state, batch):
    for key in batch["keys"]:
        if key in batch["results"]:
            continue
        if stop_requested(database, state["id"]):
            return
        candidate = state["candidates"][key]
        article = candidate["article"]
        if not complete_body(article):
            if not candidate.get("body_attempted"):
                _start_operation(
                    database,
                    state,
                    f"body:{batch['number']}:{key}:{candidate.get('body_retries', 0)}",
                    {"kind": "detail", "items": [article]},
                    content_key=key,
                )
            else:
                _metric(state, "body_failed")
                _save_result(
                    database,
                    state,
                    batch,
                    key,
                    {
                        "error": "body_failed",
                        "stage": "body",
                        "reason": article.get("read_error")
                        or "正文不完整，未作相关性判断",
                    },
                )
            return
        reason = _basic_reason(state, article, _history(database))
        if reason:
            _save_result(
                database, state, batch, key, {"error": reason, "reason": reason}
            )
            return
        if not candidate.get("body_confirmed"):
            candidate["body_confirmed"] = True
            _metric(state, "body_success")
        stages = candidate.setdefault("stages", {})
        saved_relevance = stages.get("relevance", {})
        if saved_relevance.get("status") == "completed":
            relevance = saved_relevance["result"]
        else:
            relevance = _run_model_stage(database, state, batch, key, "relevance")
            if relevance is None:
                return
            if relevance["relevance"] != "irrelevant":
                return
        if relevance["relevance"] == "irrelevant":
            result = {
                "content_key": key,
                "decision": "reject",
                "reason": relevance["reason"],
                "summary": None,
                "rejection_stage": "relevance",
            }
        else:
            saved_evaluation = stages.get("evaluation", {})
            if saved_evaluation.get("status") == "completed":
                result = saved_evaluation["result"]
            else:
                result = _run_model_stage(database, state, batch, key, "evaluation")
                if result is None:
                    return
            if result["decision"] == "reject":
                result = {**result, "rejection_stage": "preference"}
        _metric(state, result["decision"])
        _save_result(database, state, batch, key, result)
        return
    batch["status"] = "delivering"


def _register_batch(database, state, batch):
    from .zhihu_cards import build_content_card

    max_bytes = load_config().feishu.max_payload_bytes
    with sender_lock(database):
        for key in batch["keys"]:
            result = batch["results"].get(key, {})
            if result.get("decision") not in {"recommend", "uncertain"}:
                continue
            if stop_requested(database, state["id"]):
                return
            candidate = state["candidates"][key]
            article = candidate["article"]
            if (
                not isinstance(result.get("summary"), str)
                or not result["summary"].strip()
                or candidate.get("stages", {}).get("evaluation", {}).get("status")
                != "completed"
            ):
                raise LLMError("相关且准备发送的内容必须具有已完成的 Sol 摘要")
            card = build_content_card(
                article,
                snapshot_id=candidate["snapshot_id"],
                learning=batch["preference"],
                max_payload_bytes=max_bytes,
            )
            with transaction(database) as conn:
                if conn.execute(
                    "SELECT 1 FROM zhihu_link_deliveries WHERE article_key=?", (key,)
                ).fetchone():
                    continue
                if key in _history(database):
                    continue
                conn.execute(
                    "INSERT INTO zhihu_link_deliveries(article_key,scan_id,title,url,chat_id,send_uuid,message_type,card_json,snapshot_id) VALUES (?,?,?,?,?,?,'interactive',?,?)",
                    (
                        key,
                        state["id"],
                        article["title"],
                        article["url"],
                        state["chat_id"],
                        str(uuid.uuid5(uuid.UUID(state["id"]), "deliver:" + key)),
                        _json(card),
                        candidate["snapshot_id"],
                    ),
                )


def _batch_stats(database, state, batch):
    stats = Counter(
        result.get("decision", result.get("error", "unknown"))
        for result in batch["results"].values()
    )
    with closing(connect(database, read_only=True)) as conn:
        deliveries = dict(
            conn.execute(
                "SELECT article_key,status FROM zhihu_link_deliveries WHERE scan_id=?",
                (state["id"],),
            )
        )
        feedback = dict(
            conn.execute(
                """SELECT content_key,label FROM zhihu_feedback_revisions f
            WHERE revision_id=(SELECT max(n.revision_id) FROM zhihu_feedback_revisions n
                WHERE n.content_key=f.content_key)"""
            )
        )
    automatic = {
        key
        for key in batch["keys"]
        if batch["results"].get(key, {}).get("decision") in {"recommend", "uncertain"}
    }
    sent = {key for key in automatic if deliveries.get(key) == "delivered"}
    counted = Counter(deliveries[key] for key in automatic if key in deliveries)
    feedback_counts = Counter(feedback[key] for key in sent if key in feedback)
    review_counts = Counter(
        feedback[key]
        for key in batch["keys"]
        if key not in automatic and key in feedback
    )
    stats.update(
        {
            "sent": counted["delivered"],
            "pending": counted["pending"] + counted["sending"],
            "failed": counted["failed"],
            "registered": sum(counted.values()),
            "feedback_likes": feedback_counts["like"],
            "feedback_dislikes": feedback_counts["dislike"],
            "feedback_review_likes": review_counts["like"],
            "feedback_review_dislikes": review_counts["dislike"],
        }
    )
    evaluated = stats["recommend"] + stats["uncertain"] + stats["reject"]
    labelled = feedback_counts.total()
    stats["model_pass_rate"] = (
        (stats["recommend"] + stats["uncertain"]) / evaluated if evaluated else None
    )
    stats["user_pass_rate"] = feedback_counts["like"] / labelled if labelled else None
    stats["feedback_coverage"] = labelled / len(sent) if sent and labelled else None
    stats["irrelevant"] = sum(
        result.get("rejection_stage") == "relevance"
        for result in batch["results"].values()
    )
    stats["preference_rejected"] = sum(
        result.get("rejection_stage") == "preference"
        for result in batch["results"].values()
    )
    batch["stats"] = dict(stats)
    batch["model_usage"] = _model_usage(state, batch.get("id"))
    if batch.get("status") != "completed" or "metrics" not in batch:
        batch["metrics"] = {
            key: value - batch["metrics_start"].get(key, 0)
            for key, value in state["metrics"].items()
        }
    return stats


def _model_usage(state, batch_id=None):
    """Account for observed calls; unavailable token usage is never zero-filled."""
    attempts = [
        (a["stage"], a)
        for a in state.get("retained_usage_attempts", [])
        if batch_id is None or a.get("batch_id") == batch_id
    ]
    for candidate in state["candidates"].values():
        for name, stage in candidate.get("stages", {}).items():
            attempts.extend(
                (name, attempt)
                for attempt in stage.get("attempts", [])
                if batch_id is None or attempt.get("batch_id") == batch_id
            )
    if batch_id is None:
        attempts.extend(("rewrite", a) for a in state.get("rewrite_attempts", []))
        attempts.extend(("preference", a) for a in state.get("preference_attempts", []))
    usage = {}
    for name, attempt in attempts:
        metrics = attempt.get("telemetry", {})
        model = metrics.get("model")
        if not model:
            continue
        total = usage.setdefault(
            model,
            {
                "calls": 0,
                "attempts": 0,
                "failed_attempts": 0,
                "duration_seconds": 0,
                "reasoning_efforts": [],
                "service_tiers": [],
                "token_usage": None,
                "usage_reported_calls": 0,
                "stages": {},
            },
        )
        calls = metrics.get("calls", 0)
        total["calls"] += calls
        total["attempts"] += attempt.get("attempt_count", 1)
        total["failed_attempts"] += int(
            attempt.get("status") not in {"completed", "ready"}
        )
        total["duration_seconds"] += metrics.get("duration_seconds", 0)
        total["stages"][name] = total["stages"].get(name, 0) + calls
        for field, target in (
            ("reasoning_effort", "reasoning_efforts"),
            ("service_tier", "service_tiers"),
        ):
            value = metrics.get(field)
            if value and value not in total[target]:
                total[target].append(value)
        tokens = metrics.get("token_usage")
        if isinstance(tokens, dict):
            total["usage_reported_calls"] += calls
            if total["token_usage"] is None:
                total["token_usage"] = {}
            for key, value in tokens.items():
                if type(value) in {int, float}:
                    total["token_usage"][key] = total["token_usage"].get(key, 0) + value
    for total in usage.values():
        total["duration_seconds"] = round(total["duration_seconds"], 3)
        total["usage_missing_calls"] = total["calls"] - total["usage_reported_calls"]
    return usage


def refresh_feedback_stats(database, state):
    """Refresh report/card counters from current feedback without external work."""
    state["learning"] = zhihu_learning.snapshot(database)
    for batch in state["batches"]:
        _batch_stats(database, state, batch)
    with closing(connect(database, read_only=True)) as conn:
        state["preference_attempts"] = [
            {
                "version": row[0],
                "status": row[1],
                "telemetry": json.loads(row[2]).get("telemetry", {}),
            }
            for row in conn.execute(
                "SELECT version,status,model_json FROM zhihu_semantic_preferences WHERE created_at>=? ORDER BY version",
                (state["created_at"],),
            )
        ]
        feedback = dict(
            conn.execute(
                """SELECT content_key,label FROM zhihu_feedback_revisions f
            WHERE revision_id=(SELECT max(n.revision_id) FROM zhihu_feedback_revisions n
                WHERE n.content_key=f.content_key)"""
            )
        )
    cohorts = {}
    sources = {}
    for key, candidate in state["candidates"].items():
        measures = {
            "discovered": 1,
            "evaluated": int(
                candidate["status"] in {"recommend", "uncertain", "reject"}
            ),
            "passed": int(candidate["status"] in {"recommend", "uncertain"}),
            "likes": int(feedback.get(key) == "like"),
            "dislikes": int(feedback.get(key) == "dislike"),
        }
        cycle = str(candidate.get("discovered_cycle", 0))
        cohorts.setdefault(cycle, Counter()).update(measures)
        identities = set()
        for source in candidate["sources"]:
            if source.get("query"):
                identities.add("query:" + source["query"])
            if source.get("question_id"):
                identities.add("question:" + source["question_id"])
            for search in source.get("search_sources", []):
                if search.get("query"):
                    identities.add("query:" + search["query"])
        for identity in identities:
            sources.setdefault(identity, Counter()).update(measures)
    state["collection_yield"] = {k: dict(v) for k, v in cohorts.items()}
    state["source_yield"] = {k: dict(v) for k, v in sources.items()}
    state["model_usage"] = _model_usage(state)
    return state


def _notify(database, state, batch, suffix="complete"):
    from .zhihu_cards import build_batch_summary_card

    _batch_stats(database, state, batch)
    state["learning"] = zhihu_learning.snapshot(database)
    state["model_usage"] = _model_usage(state)
    card = build_batch_summary_card(
        state, batch, max_payload_bytes=load_config().feishu.max_payload_bytes
    )
    zhihu_store.enqueue_card(
        database,
        card,
        state["chat_id"],
        event_id=f"semantic:{state['id']}:{batch['number']}:{suffix}",
    )


def _finish_batch(database, state, batch):
    _register_batch(database, state, batch)
    if stop_requested(database, state["id"]):
        return
    stats = _batch_stats(database, state, batch)
    with closing(connect(database, read_only=True)) as conn:
        blocked = conn.execute(
            "SELECT 1 FROM zhihu_link_deliveries WHERE scan_id=? AND status='failed' LIMIT 1",
            (state["id"],),
        ).fetchone()
    if stats["failed"] or blocked:
        state.update(
            status="delivery_failed",
            stop_reason="delivery_failed",
            last_error={
                "kind": "delivery_failed",
                "message": "原发送队列存在失败，继续操作会复用原 UUID 重试",
            },
        )
        _notify(database, state, batch, "delivery_failed")
        return
    if stats["pending"]:
        return
    batch.update(status="completed", completed_at=_now())
    state["last_batch_metrics"] = dict(state["metrics"])
    baseline = set(state["request"].get("baseline_keys", []))
    with closing(connect(database, read_only=True)) as conn:
        registered = {
            r[0]
            for r in conn.execute(
                "SELECT article_key FROM zhihu_link_deliveries WHERE scan_id=? AND message_type='interactive'",
                (state["id"],),
            )
        }
    state["request"]["qualified"] = sum(
        key not in baseline
        and c.get("result", {}).get("decision") in {"recommend", "uncertain"}
        for key, c in state["candidates"].items()
        if key in registered
    )
    pending = _pending(database, state)
    exhausted = not pending and not _uncovered(state)
    anomalies = any(
        c["status"]
        in {"body_failed", "model_failed", "relevance_failed", "evaluation_failed"}
        for c in state["candidates"].values()
    )
    request = state["request"]
    append_pending = (
        request["action"] == "append" and request["qualified"] < request["quantity"]
    )
    state.update(
        status="exhausted" if exhausted and not anomalies else "waiting_user",
        stop_reason="results_exhausted_with_errors"
        if exhausted and anomalies
        else "results_exhausted"
        if exhausted
        else "append_waiting_user"
        if append_pending
        else "batch_completed",
    )
    _notify(database, state, batch)


def step(database, scan_id):
    """One bounded unit under the caller's scan lock; successful work is reusable."""
    from .zhihu_scan import get_scan
    from .zhihu_semantics import rewrite_topic

    state = get_scan(database, scan_id)
    if not state or state.get("schema_version") != 3:
        return
    try:
        _controls(database, state)
    except (ConfigError, ValueError, KeyError, TypeError, OSError) as exc:
        # Keep the durable round and queued command intact on control failure.
        state = get_scan(database, scan_id)
        state.update(
            status="processing_failed",
            last_error={
                "kind": "control",
                "message": f"{type(exc).__name__}: {str(exc)[:250]}",
            },
        )
        state["errors"].append({**state["last_error"], "at": _now()})
        _save(database, state)
        export_report(state)
        return state
    scan_id = state["id"]
    if state["status"] not in {"running", "stopping", "waiting_preference"}:
        refresh_feedback_stats(database, state)
        _save(database, state)
        export_report(state)
        return
    try:
        if state["status"] == "waiting_preference":
            preference = zhihu_learning.snapshot(database)
            state["learning"] = preference
            if preference["training_pending"]:
                _save(database, state)
                export_report(state)
                return state
            state.update(
                status="waiting_user",
                last_error=None,
                stop_reason="preference_updated_continue_required",
            )
            if state["batches"]:
                _notify(database, state, state["batches"][-1], "preference_ready")
            _save(database, state)
            export_report(state)
            return state
        if state["active"]:
            _poll(database, state, CollectorClient.from_env())
        elif stop_requested(database, scan_id):
            pass
        elif not state["query_plan"]:
            client = CollectorClient.from_env()
            if not {"search_page", "question_answers_page"} <= set(
                client.health().get("capabilities", [])
            ):
                raise CollectorError("protocol", "采集器需先升级为可恢复单页任务")
            started = time.monotonic()
            attempts = state.setdefault("rewrite_attempts", [])
            for old in attempts:
                if old["status"] == "running":
                    old.update(
                        status="interrupted", error="进程中断，未取得完整查询计划"
                    )
            attempt = {"status": "running", "started_at": _now(), "telemetry": {}}
            attempts.append(attempt)
            _save(database, state)
            try:
                plan = rewrite_topic(state["topic"], telemetry=attempt["telemetry"])
                install_query_plan(state, plan)
            except (LLMError, ConfigError, ValueError, TypeError, OSError) as exc:
                attempt.update(status="failed", error=str(exc)[:300])
                raise
            else:
                attempt["status"] = "completed"
            finally:
                attempt["completed_at"] = _now()
                attempt["telemetry"].setdefault(
                    "duration_seconds", round(time.monotonic() - started, 3)
                )
                _metric(state, "model_calls", attempt["telemetry"].get("calls", 0))
                _metric(
                    state, "rewrite_model_calls", attempt["telemetry"].get("calls", 0)
                )
                _metric(state, "rewrite_seconds", time.monotonic() - started)
                _save(database, state)
            state["phase"] = "collect"
        else:
            batch = (
                state["batches"][-1]
                if state["batches"] and state["batches"][-1]["status"] != "completed"
                else None
            )
            if batch:
                if batch["status"] == "evaluating":
                    _evaluate(database, state, batch)
                else:
                    _finish_batch(database, state, batch)
            else:
                if _batch_preference(database, state) is None:
                    _save(database, state)
                    export_report(state)
                    return state
                pending = _pending(database, state)
                needs_first = not state["batches"] and state["phase"] == "collect"
                if (
                    state["collection"] is not None
                    or state.get("force_collection", False)
                    or needs_first
                    or (
                        len(pending) < state["params"]["evaluation_batch_size"]
                        and _uncovered(state)
                    )
                ):
                    if not _advance(database, state):
                        _save(database, state)
                        return
                    pending = _pending(database, state)
                    state["force_collection"] = False
                    state["phase"] = "evaluate"
                    # First/continue is a trial batch even when this advance yields
                    # zero candidates; append may advance further after its report.
                _new_batch(database, state, pending)
    except CodexBusyError:
        state.update(
            status="model_busy",
            last_error={"kind": "model_busy", "message": "Codex 正忙，已有进度保留"},
        )
    except LLMError as exc:
        state.update(
            status="model_failed",
            last_error={"kind": "model_failed", "message": str(exc)[:300]},
        )
    except CollectorError as exc:
        state.update(
            status="waiting_login" if exc.kind == "login" else "collector_failed",
            last_error={"kind": exc.kind, "message": str(exc)[:300]},
        )
        state["errors"].append(
            {
                **state["last_error"],
                "at": _now(),
                "operation": state.get("active", {}).get("label")
                if state.get("active")
                else None,
            }
        )
    except (ConfigError, ValueError, KeyError, TypeError, OSError) as exc:
        state.update(
            status="collector_failed" if state.get("active") else "processing_failed",
            last_error={
                "kind": "protocol" if state.get("active") else "processing",
                "message": f"{type(exc).__name__}: {str(exc)[:250]}",
            },
        )
        state["errors"].append({**state["last_error"], "at": _now()})
    _save(database, state)
    if state["status"] in {
        "model_busy",
        "model_failed",
        "collector_failed",
        "waiting_login",
        "processing_failed",
    }:
        batch = (
            state["batches"][-1]
            if state["batches"]
            else {
                "number": 0,
                "keys": [],
                "results": {},
                "stats": {},
                "metrics_start": {},
                "preference": {},
                "params": state["params"],
            }
        )
        _notify(
            database,
            state,
            batch,
            f"error:{len(state['controls'])}:{len(state['errors'])}",
        )
    refresh_feedback_stats(database, state)
    _save(database, state)
    export_report(state)
    return state


def summary(state):
    candidates = state["candidates"]
    return {
        "scan_id": state["id"],
        "status": state["status"],
        "topic": state["topic"],
        "window_start": state["window_start"],
        "window_end": state["window_end"],
        "query_plan": state["query_plan"],
        "query_contributions": state.get("query_contributions", {}),
        "evaluation_policy": state.get("evaluation_policy", EVALUATION_POLICY),
        "experiment_round": state.get("experiment_round", 1),
        "params": state["params"],
        "candidates": len(candidates),
        "candidate_states": dict(Counter(c["status"] for c in candidates.values())),
        "batches": [
            {
                key: batch.get(key)
                for key in (
                    "id",
                    "number",
                    "keys",
                    "preference",
                    "stats",
                    "params",
                    "metrics",
                    "model_usage",
                    "started_at",
                    "completed_at",
                    "status",
                )
            }
            for batch in state["batches"]
        ],
        "remaining_searches": [q for q in state["queries"] if not q["is_end"]],
        "remaining_questions": [
            q for q in state["questions"].values() if not q["is_end"]
        ],
        "stop_reason": state["stop_reason"],
        "last_error": state["last_error"],
        "request": state["request"],
        "metrics": state["metrics"],
        "model_usage": state.get("model_usage", {}),
        "stage_results": {
            key: {
                "status": candidate["status"],
                "stages": candidate.get("stages", {}),
                "result": candidate.get("result"),
            }
            for key, candidate in candidates.items()
            if candidate.get("stages")
        },
        "controls": state["controls"],
        "learning": state.get("learning", {}),
        "collection_yield": state.get("collection_yield", {}),
        "source_yield": state.get("source_yield", {}),
        "baseline_discovery": state.get("baseline_discovery", {}),
        "parameter_note": "每次仅调整一项规模参数；偏好版本和候选差异也会影响通过率。未实测的参数保持待验证。",
        "scope": "仅排序已发现且符合基础条件的内容；未保证整个话题的最高赞覆盖。",
    }


def export_report(state):
    from .zhihu_scan import _write

    _write(Path(state["directory"]) / "semantic-report.json", summary(state))
    _write(
        Path(state["directory"]) / "scan-config.json",
        {
            key: state[key]
            for key in ("topic", "window_start", "window_end", "query_plan", "params")
        },
    )

    def rate(numerator, denominator):
        return (
            f"{numerator}/{denominator} ({numerator / denominator:.0%})"
            if denominator
            else "暂无数据"
        )

    rows = [
        "# 知乎逐批反馈实验",
        "",
        f"话题：{state['topic']['name']}",
        f"状态：{state['status']}；停止原因：{state['stop_reason']}",
        f"搜索词：{'、'.join(state['query_plan'])}",
        "",
        "| 批次 | 偏好版本 | 评价上限 | 模型放行率 | 实际送达 | 用户通过率 | 反馈覆盖率 |",
        "|---|---|---|---|---|---|---|",
    ]
    for batch in state["batches"]:
        stats = batch["stats"]
        passed = stats.get("recommend", 0) + stats.get("uncertain", 0)
        evaluated = passed + stats.get("reject", 0)
        likes = stats.get("feedback_likes", 0)
        labelled = likes + stats.get("feedback_dislikes", 0)
        sent = stats.get("sent", 0)
        rows.append(
            f"| {batch['number']} | {batch['preference'].get('label') or batch['preference'].get('version') or '空偏好'} | "
            f"{batch['params']['evaluation_batch_size']} | {rate(passed, evaluated)} | "
            f"{sent} | {rate(likes, labelled)} | {rate(labelled, sent) if labelled else '暂无数据'} |"
        )
    rows.extend(
        [
            "",
            "## 参数与采集收益",
            "",
            f"当前工程参数：`{_json(state['params'])}`。",
            "展示总量由用户逐批继续和主动结束决定。搜索词数量沿用已生成计划，未自动扩展。",
            "",
            "| 采集轮次 | 新增发现 | 已评价 | 模型放行 | 喜欢 | 不喜欢 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for cycle, counts in sorted(
        state.get("collection_yield", {}).items(), key=lambda item: int(item[0])
    ):
        label = "保留发现基线" if cycle == "0" else cycle
        rows.append(
            f"| {label} | {counts['discovered']} | {counts['evaluated']} | "
            f"{counts['passed']} | {counts['likes']} | {counts['dislikes']} |"
        )
    rows.extend(
        [
            "",
            "## 模型实际用量",
            "",
            "| 模型 | 推理档位 | 服务档位 | 调用次数 | 耗时（秒） | 已取得 token 用量 | 未取得用量的调用 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for model, usage in state.get("model_usage", {}).items():
        tokens = (
            _json(usage["token_usage"])
            if usage["token_usage"] is not None
            else "未提供"
        )
        rows.append(
            f"| {model} | {', '.join(usage['reasoning_efforts'])} | {', '.join(usage['service_tiers'])} | "
            f"{usage['calls']} | {usage['duration_seconds']} | {tokens} | {usage['usage_missing_calls']} |"
        )
    rows.extend(
        [
            "",
            "用量按实际返回值统计，失败调用保留在对应阶段；端点未提供的 token 和账单金额不作估算。",
            "",
            "## 实际搜索词贡献",
            "",
            "| 搜索词 | 新增候选 | 重复记录 | 本轮页数 | 复用页数 |",
            "|---|---|---|---|---|",
        ]
    )
    for query, counts in state.get("query_contributions", {}).items():
        label = query.replace("|", "\\|").replace("\n", " ")
        rows.append(
            f"| {label} | {counts.get('new_candidates', 0)} | {counts.get('duplicates', 0)} | "
            f"{counts.get('pages', 0)} | {counts.get('reused_pages', 0)} |"
        )
    rows.extend(
        [
            "",
            "每次只改变一项规模参数；偏好版本和候选内容变化也会影响通过率。",
            "未发生真实参数比较的项目仍待验证；模型放行率不能代替用户通过率，未反馈不计为不喜欢。",
            "源搜索词及问题贡献见同目录 semantic-report.json；多来源候选各来源单独计数，不相加作为总量。",
            "正文、模型或投递异常分别保留，不计作不推荐或耗尽。",
            "",
        ]
    )
    report = Path(state["directory"]) / "experiment-report.md"
    report.write_text("\n".join(rows), encoding="utf-8")
    return summary(state)


def _published(value):
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return datetime.fromisoformat(value).replace(tzinfo=BEIJING_TIMEZONE)
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError, TypeError:
        return None
