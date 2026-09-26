"""Topic scans: fixed default-answer lists, frozen learning, and durable provenance."""

import json
import re
import uuid
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import zhihu_learning, zhihu_store
from .config import ConfigError, resolve_feishu_delivery
from .database import connect
from .datetime_utils import BEIJING_TIMEZONE
from .locking import sender_lock
from .zhihu_client import CollectorClient, CollectorError
from .zhihu_verify import complete_body

TERMINAL = {"completed", "partial_failed", "failed"}


def create_topic_scan(database, topic_id, request_uuid=None):
    from . import zhihu_delivery
    from .zhihu_scan import _json, _write

    scan_id = str(uuid.UUID(request_uuid)) if request_uuid else str(uuid.uuid4())
    with sender_lock(database):
        zhihu_delivery.initialize(database)
        zhihu_store.initialize(database)
    with closing(connect(database)) as conn:
        previous = conn.execute(
            "SELECT state_json FROM zhihu_scans WHERE id=?", (scan_id,)
        ).fetchone()
        if previous:
            return json.loads(previous[0])
    topic = zhihu_store.get_topic(database, topic_id)
    if not topic or not topic["enabled"]:
        raise ConfigError("话题不存在或已停用")
    health = CollectorClient.from_env().health()
    if "question_answers" not in health.get("capabilities", []):
        raise ConfigError("请先升级采集器：缺少 question_answers 能力")
    delivery = resolve_feishu_delivery()
    if delivery.receive_id_type != "chat_id":
        raise ConfigError("知乎卡片必须发送到飞书群 chat_id")
    now = datetime.now(UTC)
    directory = Path(database).resolve().parent / "zhihu-runs" / scan_id
    (directory / "raw").mkdir(parents=True, exist_ok=True, mode=0o700)
    state = {
        "schema_version": 2,
        "id": scan_id,
        "created_at": now.isoformat(),
        "status": "running",
        "auto_notify": True,
        "chat_id": delivery.receive_id,
        "topic": topic,
        "learning": zhihu_learning.snapshot(database),
        "config": {
            "scan": {
                "queries": [
                    {"query": term, "sort": order, "max_pages": 3}
                    for term in dict.fromkeys(
                        term.strip().casefold() for term in topic["search_terms"]
                    )
                    for order in ("latest", "general")
                ],
                "max_unique": 200,
                "max_results": 10,
                "detail_batch_size": 20,
            }
        },
        "directory": str(directory),
        "query_index": 0,
        "seeds": [],
        "candidates": [],
        "records": [],
        "questions": {},
        "resolved_seeds": [],
        "checked": {},
        "coverage": [],
        "operations": [],
        "errors": [],
        "active": None,
        "stop_reason": "",
        "last_error": None,
        "selected_keys": [],
        "registered_links": 0,
    }
    with closing(connect(database)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO zhihu_scans VALUES (?,?,?,?)",
            (scan_id, state["created_at"], state["status"], _json(state)),
        )
        state = json.loads(
            conn.execute(
                "SELECT state_json FROM zhihu_scans WHERE id=?", (scan_id,)
            ).fetchone()[0]
        )
    _write(
        directory / "scan-config.json",
        {
            **state["config"],
            "topic": state["topic"],
            "learning": state["learning"],
            "window_end": state["created_at"],
        },
    )
    return state


def delivery_keys(database):
    """Successful historical sends and every reserved identity block re-enqueueing."""
    with closing(connect(database, read_only=True)) as conn:
        result = dict(
            conn.execute("SELECT article_key,status FROM zhihu_link_deliveries")
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='rule_test_deliveries'"
        ).fetchone():
            for (key,) in conn.execute(
                "SELECT article_key FROM rule_test_deliveries WHERE status='delivered'"
            ):
                result[key] = "delivered"
        return result


def _operation(state, label, payload, purpose=None):
    payload["request_uuid"] = str(uuid.uuid5(uuid.UUID(state["id"]), label))
    return {
        "payload": payload,
        "collector_id": None,
        "label": label,
        "purpose": purpose or payload["kind"],
    }


def _question(state, article):
    qid = article.get("question_id")
    if not qid or article["content_type"] != "answer":
        return
    question = state["questions"].setdefault(
        qid,
        {
            "question_id": qid,
            "status": "pending",
            "seed_keys": [],
            "ranked_answers": [],
            "coverage": [],
            "collected_at": None,
            "request_uuid": str(uuid.uuid5(uuid.UUID(state["id"]), f"question:{qid}")),
        },
    )
    if article["article_key"] not in question["seed_keys"]:
        question["seed_keys"].append(article["article_key"])


def _record_map(state):
    return {row["article_key"]: row for row in state["records"]}


def _next(database, state):
    scan = state["config"]["scan"]
    index = state["query_index"]
    if index < len(scan["queries"]):
        return _operation(
            state, f"search:{index}", {"kind": "search", **scan["queries"][index]}
        )
    by_key = {row["article_key"]: row for row in state["candidates"]}
    for key in state["seeds"]:
        article = by_key[key]
        _question(state, article)
        if (
            article["content_type"] == "answer"
            and not article.get("question_id")
            and key not in state["resolved_seeds"]
        ):
            return _operation(
                state,
                f"resolve:{key}",
                {"kind": "detail", "items": [article]},
                "seed_detail",
            )
    for qid, question in state["questions"].items():
        if question["status"] == "pending":
            return _operation(
                state,
                f"question:{qid}",
                {
                    "kind": "question_answers",
                    "question_id": qid,
                    "limit": 20,
                    "sort": "default",
                },
            )
    # All answer lists are fixed before delivery, date, or preference filtering.
    history = delivery_keys(database)
    records = _record_map(state)
    pending = []
    for article in state["candidates"]:
        key = article["article_key"]
        if key in state["checked"]:
            continue
        if key in history:
            state["checked"][key] = {
                "reason": "historical_delivered"
                if history[key] == "delivered"
                else "delivery_reserved",
                "score": None,
            }
        elif key in records:
            state["checked"][key] = evaluate(records[key], state)
        else:
            pending.append(article)
    if pending:
        items = pending[: scan["detail_batch_size"]]
        return _operation(
            state,
            "detail:" + ",".join(a["article_key"] for a in items),
            {"kind": "detail", "items": items},
        )
    state["stop_reason"] = "candidates_evaluated"
    return None


def _published(value):
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return datetime.fromisoformat(value).replace(tzinfo=BEIJING_TIMEZONE)
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError, TypeError:
        return None


def evaluate(article, state):
    result = {
        "reason": "eligible",
        "score": None,
        "model_version": state["learning"]["version"],
    }
    if not complete_body(article):
        result["reason"] = (
            "body_incomplete" if article["status"] != "failed" else "body_failed"
        )
        return result
    published = _published(article["published_at"])
    window = state["topic"]["time_range"]
    end = datetime.fromisoformat(state["created_at"])
    if window != "all":
        if published is None:
            result["reason"] = "date_unknown"
            return result
        if published < end - timedelta(days=int(window.removesuffix("d"))):
            result["reason"] = "date_expired"
            return result
        if published > end:
            result["reason"] = "date_future"
            return result
    result["score"] = zhihu_learning.score(article["body"], state["learning"])
    if result["score"] < state["learning"]["threshold"]:
        result["reason"] = "keyword_filtered"
    return result


def _snapshot(database, state, article):
    if complete_body(article):
        saved = zhihu_store.save_content(database, article, scan_id=state["id"])
        article["snapshot_id"] = saved["snapshot_id"]
    records = _record_map(state)
    records[article["article_key"]] = article
    state["records"] = list(records.values())


def _provenance(database, state, article, discovery):
    zhihu_store.save_discovery(
        database,
        state["id"],
        state["topic"]["id"],
        article["article_key"],
        discovery["source"],
        search_term=discovery["search_term"],
        seed_answer_id=discovery.get("seed_answer_id", ""),
        question_id=discovery.get("question_id", ""),
        rank=discovery.get("list_rank"),
        details=discovery,
    )


def _merge(database, state, article, discovery):
    previous = next(
        (a for a in state["candidates"] if a["article_key"] == article["article_key"]),
        None,
    )
    if previous is None:
        previous = article
        previous["discoveries"] = []
        state["candidates"].append(previous)
    if discovery not in previous["discoveries"]:
        previous["discoveries"].append(discovery)
    if not previous.get("question_id") and article.get("question_id"):
        previous["question_id"] = article["question_id"]
    _provenance(database, state, previous, discovery)
    return previous


def _consume(database, state, client, active, result, response):
    from .zhihu_scan import _record

    records = response.get("records")
    coverage = response.get("coverage", result.get("coverage", []))
    if not isinstance(records, list) or not isinstance(coverage, list):
        raise CollectorError("protocol", "采集器缺少 records/coverage 数组")
    operation = {
        "id": active["collector_id"],
        "kind": active["payload"]["kind"],
        "purpose": active["purpose"],
        "status": result["status"],
        "coverage": coverage,
        "error": result.get("error"),
    }
    if operation["status"] != "completed":
        state["errors"].append(operation)
    kind = active["purpose"]
    if kind == "search":
        query = state["config"]["scan"]["queries"][state["query_index"]]
        accepted, capped = 0, 0
        for raw in records:
            key = f"zhihu:{raw.get('content_type')}:{raw.get('content_id')}"
            if key not in state["seeds"] and len(state["seeds"]) >= 200:
                capped += 1
                continue
            article = _record(client, state, raw)
            article["first_discovery"]["query_index"] = state["query_index"] + 1
            if article["article_key"] not in state["seeds"]:
                state["seeds"].append(article["article_key"])
                accepted += 1
            discovery = {
                "source": "search",
                "search_term": query["query"],
                "sort": query["sort"],
                "query_index": state["query_index"] + 1,
                "seed_answer_id": article["content_id"]
                if article["content_type"] == "answer"
                else "",
                "question_id": article.get("question_id", ""),
                "position": article["first_discovery"],
                "collected_at": article["fetched_at"],
            }
            _merge(database, state, article, discovery)
            _question(state, article)
        state["coverage"].append(
            {
                **query,
                "kind": "search",
                "collector": coverage,
                "status": result["status"],
                "accepted_seeds": accepted,
                "seed_limit_excluded": capped,
            }
        )
        state["query_index"] += 1
    elif kind == "question_answers":
        qid = active["payload"]["question_id"]
        question = state["questions"][qid]
        if len(records) > 20:
            raise CollectorError("protocol", "默认回答列表超过20个")
        keys, ordered = set(), []
        candidates = {a["article_key"]: a for a in state["candidates"]}
        seed = candidates[question["seed_keys"][0]]
        for rank, raw in enumerate(records, 1):
            if (
                raw.get("question_id") != qid
                or raw.get("list_rank") != rank
                or raw.get("content_type") != "answer"
            ):
                raise CollectorError("protocol", "同题回答 ID 或默认排名不一致")
            article = _record(
                client,
                state,
                {
                    **raw,
                    "first_discovery": {
                        **seed["first_discovery"],
                        "list_rank": rank,
                        "source": "question_answers",
                    },
                },
            )
            key = article["article_key"]
            if key in keys:
                raise CollectorError("protocol", "默认回答列表含重复回答")
            keys.add(key)
            ordered.append(
                {
                    "article_key": key,
                    "answer_id": article["content_id"],
                    "list_rank": rank,
                    "fetched_at": article["fetched_at"],
                }
            )
            article["first_discovery"] = {
                **seed["first_discovery"],
                "list_rank": rank,
                "source": "question_answers",
            }
            for seed_key in question["seed_keys"]:
                source = candidates[seed_key]
                for origin in source["discoveries"]:
                    if origin["source"] != "search":
                        continue
                    _merge(
                        database,
                        state,
                        article,
                        {
                            "source": "question_answers",
                            "search_term": origin["search_term"],
                            "seed_answer_id": source["content_id"],
                            "question_id": qid,
                            "list_rank": rank,
                            "collected_at": article["fetched_at"],
                            "sort": "default",
                        },
                    )
            # Some default-list responses include a verified full normal answer.
            # Reuse that body, but still defer all filtering until every list is fixed.
            if complete_body(article):
                merged = next(a for a in state["candidates"] if a["article_key"] == key)
                article["discoveries"] = list(merged["discoveries"])
                _snapshot(database, state, article)
        complete = (
            result["status"] == "completed"
            and len(coverage) == 1
            and coverage[0].get("complete") is True
            and coverage[0].get("actual_count") == len(ordered)
            and coverage[0].get("stop_reason")
            in {"limit", "limit_reached", "no_next_page", "exhausted", "end_of_list"}
        )
        if not complete:
            state["errors"].append(
                {
                    "kind": "expansion_incomplete",
                    "question_id": qid,
                    "coverage": coverage,
                }
            )
        question.update(
            status="completed" if complete else "partial_failed",
            ranked_answers=ordered,
            coverage=coverage,
            collected_at=datetime.now(UTC).isoformat(),
        )
        zhihu_store.save_expansion(
            database,
            state["id"],
            qid,
            question["request_uuid"],
            status=question["status"],
            ranked_answers=ordered,
            progress={
                "coverage": coverage,
                "collector_run_id": active["collector_id"],
                "collected_at": question["collected_at"],
            },
            error="" if complete else "default_list_incomplete",
        )
        state["coverage"].append(
            {
                "kind": kind,
                "question_id": qid,
                "status": question["status"],
                "collector": coverage,
            }
        )
    else:
        by_key = {(r.get("content_type"), r.get("content_id")): r for r in records}
        expected = {
            (a["content_type"], a["content_id"]) for a in active["payload"]["items"]
        }
        if len(by_key) != len(records) or set(by_key) != expected:
            raise CollectorError("protocol", "详情返回 ID 与请求不一致")
        for candidate in active["payload"]["items"]:
            article = _record(
                client,
                state,
                by_key[(candidate["content_type"], candidate["content_id"])],
            )
            article["first_discovery"] = candidate["first_discovery"]
            article["discoveries"] = list(candidate.get("discoveries", []))
            article["question_id"] = article.get("question_id") or candidate.get(
                "question_id", ""
            )
            _snapshot(database, state, article)
            if kind == "seed_detail":
                state["resolved_seeds"].append(article["article_key"])
                for old in state["candidates"]:
                    if old["article_key"] == article["article_key"]:
                        old["question_id"] = article["question_id"]
                        for discovery in old["discoveries"]:
                            discovery["question_id"] = article["question_id"]
                            _provenance(database, state, old, discovery)
                _question(state, article)
                if not article.get("question_id"):
                    state["errors"].append(
                        {
                            "kind": "question_id_missing",
                            "article_key": article["article_key"],
                        }
                    )
            else:
                state["checked"][article["article_key"]] = evaluate(article, state)
    state["operations"].append(operation)


def tick(database, state, client):
    from .zhihu_scan import _fetch_evidence, _save

    if state["status"] in TERMINAL:
        return
    if state["active"] is None:
        state["active"] = _next(database, state)
        if state["active"] is None:
            finish(database, state)
            _save(database, state)
            export_report(state)
            return
        _save(database, state)
    active = state["active"]
    if active["payload"]["kind"] == "question_answers":
        question = state["questions"][active["payload"]["question_id"]]
        zhihu_store.save_expansion(
            database,
            state["id"],
            question["question_id"],
            question["request_uuid"],
            status="running",
            progress={
                "collector_run_id": active["collector_id"],
                "coverage": active.get("remote_coverage", []),
            },
        )
    if active["collector_id"] is None:
        result = client.start(active["payload"])
        active["collector_id"] = str(uuid.UUID(result["id"]))
        _save(database, state)
        return
    result = client.run(active["collector_id"])
    status = result.get("status")
    active["remote_status"] = status
    active["remote_coverage"] = result.get("coverage", [])
    active["remote_error"] = result.get("error")
    active["evidence_files"] = _fetch_evidence(
        client, state, result.get("evidence", [])
    )
    if status == "waiting_login":
        login = client.login_status()
        active["login_status"], active["login_error"] = (
            login.get("status"),
            login.get("error"),
        )
        state["status"] = "waiting_login"
        _save(database, state)
        export_report(state)
        return
    active.pop("login_status", None)
    active.pop("login_error", None)
    if status in {"running", "queued"}:
        state["status"] = "running"
        _save(database, state)
        return
    if status not in TERMINAL:
        raise CollectorError("protocol", "未知采集任务状态")
    _consume(
        database, state, client, active, result, client.records(active["collector_id"])
    )
    state.update(
        active=None, status="running", retry_count=0, next_retry_at=0, last_error=None
    )
    _save(database, state)
    export_report(state)


def finish(database, state):
    records = _record_map(state)
    # Recheck delivery reservations after body acquisition, across topics and restarts.
    history = delivery_keys(database)
    for article in state["candidates"]:
        key = article["article_key"]
        if key in history:
            state["checked"][key] = {
                "reason": "historical_delivered"
                if history[key] == "delivered"
                else "delivery_reserved",
                "score": None,
            }
        result = state["checked"].get(key, {"reason": "not_evaluated", "score": None})
        body = records.get(key, article)
        zhihu_store.save_candidate_result(
            database,
            state["id"],
            state["topic"]["id"],
            body,
            snapshot_id=body.get("snapshot_id"),
            reason=result["reason"],
            score=result.get("score"),
            model_version=state["learning"]["version"],
        )
    selected = [
        records[key]
        for key, result in state["checked"].items()
        if result["reason"] == "eligible" and key in records
    ]
    selected.sort(
        key=lambda a: (
            -state["checked"][a["article_key"]]["score"],
            -(
                _published(a["published_at"]).timestamp()
                if _published(a["published_at"])
                else float("-inf")
            ),
            a["article_key"],
        )
    )
    # Keep the entire ranking so a concurrently reserved identity never consumes quota.
    state["selected_keys"] = [a["article_key"] for a in selected]
    state["status"] = (
        "partial_failed"
        if state["errors"]
        or any(
            v["reason"] in {"body_failed", "body_incomplete", "not_evaluated"}
            for v in state["checked"].values()
        )
        else "completed"
    )


def summary(state):
    reasons = Counter(value["reason"] for value in state["checked"].values())
    return {
        "id": state["id"],
        "schema_version": 2,
        "status": state["status"],
        "topic": state["topic"]["name"],
        "topic_id": state["topic"]["id"],
        "directory": state["directory"],
        "window_end": state["created_at"],
        "time_range": state["topic"]["time_range"],
        "learning_version": state["learning"]["version"],
        "learning_ready": state["learning"]["ready"],
        "learning_progress": state["learning"]["progress"],
        "search_seeds": len(state["seeds"]),
        "expansion_questions": len(state["questions"]),
        "expansion_completed": sum(
            q["status"] == "completed" for q in state["questions"].values()
        ),
        "expansion_incomplete": sum(
            q["status"] != "completed" for q in state["questions"].values()
        ),
        "same_question_candidates": sum(
            len(q["ranked_answers"]) for q in state["questions"].values()
        ),
        "unique_candidates": len(state["candidates"]),
        "historical_delivered": reasons["historical_delivered"],
        "body_success": sum(complete_body(a) for a in state["records"]),
        "details_checked": len(state["records"]),
        "filter_reasons": dict(reasons),
        "registered_links": state.get("registered_links", 0),
        "coverage": state["coverage"],
        "collector_run_id": (state["active"] or {}).get("collector_id"),
        "active_coverage": (state["active"] or {}).get("remote_coverage", []),
        "login_status": (state["active"] or {}).get("login_status"),
        "errors": state["errors"],
        "last_error": state["last_error"],
        "stop_reason": state["stop_reason"],
    }


def export_report(state):
    from .zhihu_scan import _json, _write

    directory = Path(state["directory"])
    report = {
        "summary": summary(state),
        "learning": state["learning"],
        "questions": state["questions"],
        "results": state["checked"],
    }
    _write(
        directory / "collection.json",
        {
            "schema_version": 2,
            "records": state["records"],
            "candidates": state["candidates"],
            "status": state["status"],
            "coverage": state["coverage"],
            "errors": state["errors"],
        },
    )
    _write(directory / "scores.json", report)
    (directory / "scores.md").write_text(
        "# 知乎话题扫描\n\n```json\n" + _json(report) + "\n```\n", encoding="utf-8"
    )
    return report
