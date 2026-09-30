"""Resumable, owner-driven Zhihu search, semantic evaluation and delivery.

The scan JSON is the durable journal: every external request has a saved UUID,
each request preserves its delivery quota and preference, and callbacks only
append control jobs. No SQLite transaction spans collector/model/Feishu I/O.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
import uuid
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import zhihu_delivery, zhihu_learning, zhihu_store
from .codex_runtime import LLMError
from .config import ConfigError, load_config, resolve_feishu_delivery
from .database import connect, transaction
from .datetime_utils import BEIJING_TIMEZONE
from .locking import sender_lock
from .storage import FeedbackError
from .zhihu_client import CollectorError
from .zhihu_query_plan import (
    EVALUATION_POLICY,
)
from .zhihu_verify import complete_body

DEFAULT_PARAMS = {"model_request_size": 1}


def parameters():
    path = Path(os.environ.get("SCOUT_ZHIHU_PARAMS", "config.zhihu-semantic.toml"))
    supplied = tomllib.loads(path.read_text()) if path.exists() else {}
    values = dict(supplied.get("semantic", DEFAULT_PARAMS))
    if set(supplied) - {"semantic"} or values != DEFAULT_PARAMS:
        raise ConfigError("仅支持 semantic.model_request_size = 1，未知配置不受支持")
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
            if previous.get("schema_version") != 5:
                raise ConfigError("旧扫描已终止，请重新发起搜索")
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
        "schema_version": 5,
        "evaluation_policy": EVALUATION_POLICY,
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
        "candidates": {},
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
        "stop_requested": False,
    }
    with transaction(database) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO zhihu_scans VALUES (?,?,?,?)",
            (scan_id, state["created_at"], state["status"], _json(state)),
        )
    from .zhihu_stream import initialize

    initialize(database, state)
    return state


def _resolve_scan(conn, scan_id):
    row = conn.execute(
        "SELECT state_json FROM zhihu_scans WHERE id=?", (scan_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def request_control(
    database,
    scan_id,
    action,
    quantity=None,
    event_id="",
):
    """Callback-safe command persistence; stop is visible even during model I/O."""
    scan_id = str(uuid.UUID(scan_id))
    if action not in {"continue", "append", "stop"}:
        raise FeedbackError("未知扫描操作")
    if action == "append" and (type(quantity) is not int or not 1 <= quantity <= 10000):
        raise FeedbackError("追加数量需为 1–10000 的整数")
    payload = {
        "scan_id": scan_id,
        "action": action,
        "quantity": quantity,
    }
    with transaction(database, rows=True, timeout=0.3) as conn:
        current = _resolve_scan(conn, scan_id)
        if not current or current.get("schema_version") != 5:
            raise FeedbackError("旧扫描已终止，请重新发起搜索")
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
        stamp = _now()
        if action != "stop":
            # Capture the effective version at the owner's explicit request.
            # The write transaction excludes concurrent preference commits.
            payload["preference"] = zhihu_learning.snapshot(database)
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
        in_flight = any(
            state.get("stream", {}).get("pipeline", {}).get("active", {}).values()
        )
        state["status"] = "stopping" if in_flight else "stopped"
        state["stop_reason"] = "user_stop"
    with transaction(database) as conn:
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
    return existing


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
    if kind == "search_page":
        page = response.get("page", result.get("page"))
        if not isinstance(page, dict) or type(page.get("is_end")) is not bool:
            raise CollectorError("protocol", "分页任务缺少真实结束标志")
        if not page["is_end"] and not page.get("next_cursor"):
            raise CollectorError("protocol", "未结束分页缺少后续游标")
        stream = state["queries"][active["stream_index"]]
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
        else:
            if article["article_key"] != active["content_key"]:
                raise CollectorError("protocol", "详情身份与请求不一致")
        candidate = _merge(database, state, article, source)
        if kind == "detail":
            candidate["body_attempted"] = True
    if kind == "detail" and not records:
        state["candidates"][active["content_key"]]["body_attempted"] = True
    if stream is not None:
        stream.update(
            cursor=page.get("next_cursor"),
            is_end=page["is_end"],
            pages=stream["pages"] + 1,
        )
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


def _save_result(database, state, journal, key, result):
    candidate = state["candidates"][key]
    candidate["result_preference"] = dict(
        candidate.get("stages", {})
        .get("content", {})
        .get("preference", journal["preference"])
    )
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
        evaluation_policy=EVALUATION_POLICY,
        semantic_preference_version=candidate["result_preference"].get("version"),
    )
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
        model_version=candidate["result_preference"].get("version"),
    )
    journal["results"][key] = result
    _save(database, state)


def _register_ready(database, state, journal):
    from .zhihu_cards import build_content_card

    max_bytes = load_config().feishu.max_payload_bytes
    with sender_lock(database):
        for key in journal["keys"]:
            result = journal["results"].get(key, {})
            if result.get("decision") not in {"recommend", "uncertain"}:
                continue
            if stop_requested(database, state["id"]):
                return
            candidate = state["candidates"][key]
            article = candidate["article"]
            if (
                not isinstance(result.get("summary"), str)
                or not result["summary"].strip()
                or candidate.get("stages", {}).get("content", {}).get("status")
                != "completed"
            ):
                raise LLMError("准备发送的内容必须具有已完成的内容评价及摘要")
            card = build_content_card(
                article,
                snapshot_id=candidate["snapshot_id"],
                learning=journal["preference"],
                scan_id=state["id"],
                max_payload_bytes=max_bytes,
            )
            with transaction(database) as conn:
                existing = conn.execute(
                    "SELECT scan_id,status FROM zhihu_link_deliveries WHERE article_key=?",
                    (key,),
                ).fetchone()
                if existing:
                    if existing[0] != state["id"]:
                        candidate["status"] = (
                            "historical_delivered"
                            if existing[1] == "delivered"
                            else "delivery_reserved"
                        )
                    continue
                history = _history(database)
                if key in history:
                    candidate["status"] = (
                        "historical_delivered"
                        if history[key] == "delivered"
                        else "delivery_reserved"
                    )
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

    _save(database, state)


def _model_usage(state):
    """Account for observed calls; unavailable token usage is never zero-filled."""
    attempts = [
        ("content", attempt)
        for candidate in state["candidates"].values()
        for attempt in candidate.get("stages", {})
        .get("content", {})
        .get("attempts", [])
    ]
    attempts.extend(("rewrite", a) for a in state.get("rewrite_attempts", []))
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
    from .zhihu_stream import refresh

    if state.get("schema_version") == 5:
        state["learning"] = zhihu_learning.snapshot(database)
        refresh(database, state)
    return state


def step(database, scan_id, *, shutdown=None):
    from .zhihu_scan import get_scan
    from .zhihu_stream import step as stream_step

    state = get_scan(database, scan_id)
    if state and state.get("schema_version") == 5:
        return stream_step(database, state, shutdown)
    return None


def summary(state):
    if state.get("schema_version") != 5:
        return {
            "id": state["id"],
            "status": "stopped",
            "reason": "旧扫描已终止，请重新发起搜索",
        }
    return {k: v for k, v in state.items() if k != "candidates"}


def export_report(state):
    if state.get("schema_version") == 5:
        from .zhihu_stream import export_report as export_stream

        return export_stream(state)


def _published(value):
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return datetime.fromisoformat(value).replace(tzinfo=BEIJING_TIMEZONE)
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError, TypeError:
        return None
