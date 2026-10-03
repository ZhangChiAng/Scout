"""Demand-driven search and per-article processing with durable delivery quotas.

The event loop is the sole journal writer. Collector threads return immutable
responses; model tasks return results. Feishu delivery has its own worker.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path

from . import zhihu_delivery, zhihu_learning, zhihu_store
from . import zhihu_pipeline as pipeline
from . import zhihu_semantic_scan as scan
from .codex_runtime import CodexBusyError, CodexRuntime, CodexSession, LLMError
from .config import ConfigError, load_config
from .database import connect, transaction
from .llm import load_required_model_config
from .locking import RunLockedError
from .zhihu_client import CollectorClient, CollectorError
from .zhihu_query_plan import EVALUATION_POLICY, install_query_plan
from .zhihu_semantics import rewrite_topic
from .zhihu_verify import complete_body

DEFAULT_TARGET = 5
LOW_WATER = 2


def deliveries(database, state):
    with closing(connect(database, read_only=True)) as conn:
        return {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT article_key,status,delivered_at FROM zhihu_link_deliveries WHERE scan_id=? ORDER BY position",
                (state["id"],),
            )
        }


def new_request(database, state, action="continue"):
    previous = state.get("request")
    if previous and previous.get("id"):
        state.setdefault("requests", []).append(copy.deepcopy(previous))
    state["request"] = {
        "id": str(uuid.uuid4()),
        "action": action,
        "quantity": DEFAULT_TARGET,
        "baseline_keys": [
            key
            for key, value in deliveries(database, state).items()
            if value[0] == "delivered"
        ],
        "started_at": scan._now(),
        "qualified": 0,
        "preference": copy.deepcopy(state["stream"]["preference"]),
    }


def initialize(database, state):
    if (
        state.get("schema_version") != 5
        or state.get("evaluation_policy") != EVALUATION_POLICY
    ):
        raise ConfigError("旧扫描已终止，请重新发起搜索")
    if state.get("stream"):
        return
    state["search_index"] = 0
    state["stream"] = {
        "keys": [],
        "results": {},
        "preference": zhihu_learning.snapshot(database),
        "pipeline": {"active": {"content": 0}},
    }
    new_request(database, state, action="first")
    scan._save(database, state)


def refresh(database, state):
    request = state["request"]
    rows = deliveries(database, state)
    eligible = [(k, v) for k, v in rows.items() if k not in request["baseline_keys"]]
    current = dict(eligible[: request["quantity"]])
    counts = Counter(v[0] for v in current.values())
    request.update(
        qualified=counts["delivered"],
        reserved=len(current),
        pending=counts["pending"] + counts["sending"],
        failed=counts["failed"],
    )
    times = [v[1] for v in current.values() if v[0] == "delivered" and v[1]]
    if times:
        from datetime import datetime

        start = datetime.fromisoformat(request["started_at"]).timestamp()
        request["first_delivery_seconds"] = round(min(times) - start, 3)
        if counts["delivered"] >= request["quantity"]:
            request["target_delivery_seconds"] = round(max(times) - start, 3)
    state["model_usage"] = scan._model_usage(state)
    state["candidate_counts"] = dict(
        Counter(c["status"] for c in state["candidates"].values())
    )
    return rows


def controls(database, state):
    with closing(connect(database, read_only=True, rows=True)) as conn:
        jobs = conn.execute(
            "SELECT * FROM zhihu_jobs WHERE kind='semantic_control' AND status='pending' AND json_extract(payload_json,'$.scan_id')=? ORDER BY id",
            (state["id"],),
        ).fetchall()
    for job in jobs:
        command = json.loads(job["payload_json"])
        if command["action"] not in {"continue", "stop"}:
            with transaction(database) as conn:
                conn.execute(
                    "UPDATE zhihu_jobs SET status='cancelled',last_error='removed_zhihu_append',updated_at=? WHERE id=?",
                    (scan._now(), job["id"]),
                )
            continue
        state["controls"].append(
            {**command, "job_id": job["id"], "at": job["created_at"]}
        )
        if command["action"] == "stop":
            state.update(status="stopped", stop_reason="user_stop", stop_requested=True)
        elif state["status"] not in {"running"}:
            refresh(database, state)
            if state["request"]["qualified"] >= state["request"]["quantity"]:
                new_request(database, state)
            state["resume_sequence"] = state.get("resume_sequence", 0) + 1
            state["stream"]["preference"] = copy.deepcopy(command["preference"])
            state["request"]["preference"] = copy.deepcopy(
                state["stream"]["preference"]
            )
            if (state.get("active") or {}).get("terminal_failure"):
                state["active"]["resume_requested"] = True
                state["active"].pop("terminal_failure", None)
            state.update(
                status="running", stop_reason="", last_error=None, stop_requested=False
            )
            # Global faults may have marked their one interrupted article failed.
            # Explicit resume retries only that stage, never every failed article.
            for candidate in state["candidates"].values():
                for stage in candidate.get("stages", {}).values():
                    if stage.pop("retry_on_continue", False):
                        stage["status"] = "interrupted"
                        candidate.pop("result", None)
                        candidate["status"] = "pending"
                        key = candidate["article"]["article_key"]
                        state["stream"]["results"].pop(key, None)
            zhihu_delivery.reset_failed(database, state["id"])
        with transaction(database) as conn:
            if command["action"] != "stop" and state["status"] == "running":
                conn.execute(
                    "DELETE FROM zhihu_settings WHERE key=?",
                    ("semantic_stop:" + state["id"],),
                )
            conn.execute(
                "UPDATE zhihu_jobs SET status='completed',updated_at=? WHERE id=?",
                (scan._now(), job["id"]),
            )
            conn.execute(
                "UPDATE zhihu_scans SET status=?,state_json=? WHERE id=?",
                (state["status"], scan._json(state), state["id"]),
            )


def pending(database, state):
    history = scan._history(database)
    result = []
    for key, candidate in state["candidates"].items():
        if candidate["status"] != "pending":
            continue
        reason = scan._basic_reason(state, candidate["article"], history)
        if reason:
            candidate.update(status=reason, reason=reason)
            scan._metric(state, reason)
            zhihu_store.save_candidate_result(
                database,
                state["id"],
                state["topic"]["id"],
                candidate["article"],
                reason=reason,
            )
        else:
            result.append(key)
    return result


def register_ready(database, state):
    rows = refresh(database, state)
    available = state["request"]["quantity"] - state["request"]["reserved"]
    if available <= 0 or any(v[0] == "failed" for v in rows.values()):
        return
    journal = state["stream"]
    for key in journal["results"]:
        if available <= 0 or scan.stop_requested(database, state["id"]):
            break
        if key in rows or state["candidates"][key]["status"] not in {
            "recommend",
            "uncertain",
        }:
            continue
        try:
            scan._register_ready(
                database,
                state,
                {
                    **journal,
                    "keys": [key],
                    "preference": state["candidates"][key].get(
                        "result_preference", journal["preference"]
                    ),
                },
            )
        except RunLockedError:
            return
        updated = deliveries(database, state)
        if key in updated:
            available -= 1
        rows = updated
    refresh(database, state)


class Collected:
    """Already-fetched responses consumed synchronously by the journal owner."""

    def __init__(self, result, records, evidence):
        self.result, self.response, self.blobs = result, records, evidence

    def run(self, run_id):
        return self.result

    def records(self, run_id):
        return self.response

    def evidence(self, path):
        return self.blobs[path]


async def collect(active):
    client = CollectorClient.from_env()
    if not active.get("collector_id"):
        value = await asyncio.to_thread(client.start, active["payload"])
        return ("started", value["id"])
    if active.get("resume_requested"):
        await asyncio.to_thread(client.resume, active["collector_id"])
        return ("resumed", None)
    result = await asyncio.to_thread(client.run, active["collector_id"])
    records, evidence = {}, {}
    if result["status"] in {"completed", "partial_failed"}:
        records = await asyncio.to_thread(client.records, active["collector_id"])
        for raw in records.get("records", []):
            for entry in raw.get("evidence", []):
                path = entry["path"]
                if path not in evidence:
                    evidence[path] = await asyncio.to_thread(client.evidence, path)
    return ("polled", Collected(result, records, evidence))


def schedule_collection(database, state, waiting):
    # Fetch only the next body needed by a free content slot. Do not download
    # the whole page before the first model call.
    journal = state["stream"]
    active_count = journal["pipeline"]["active"]["content"]
    ready = 0
    for key in waiting:
        if not complete_body(state["candidates"][key]["article"]):
            break
        ready += 1
    if active_count + ready < pipeline.CONTENT_CONCURRENCY:
        for key in waiting:
            candidate = state["candidates"][key]
            if not complete_body(candidate["article"]) and not candidate.get(
                "body_attempted"
            ):
                scan._start_operation(
                    database,
                    state,
                    "body:stream:" + key,
                    {"kind": "detail", "items": [candidate["article"]]},
                    content_key=key,
                )
                return
    if len(waiting) > LOW_WATER:
        return
    queries = state["queries"]
    for offset in range(len(queries)):
        index = (state["search_index"] + offset) % len(queries)
        query = queries[index]
        if not query["is_end"]:
            state["search_index"] = (index + 1) % len(queries)
            scan._start_operation(
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
            return


def notify(database, state):
    from .zhihu_cards import build_stream_card

    refresh(database, state)
    suffix = (
        f"{state['request']['id']}:{state.get('resume_sequence', 0)}:{state['status']}"
    )
    zhihu_store.enqueue_card(
        database,
        build_stream_card(
            state, max_payload_bytes=load_config().feishu.max_payload_bytes
        ),
        state["chat_id"],
        event_id=f"stream:{state['id']}:{suffix}",
    )


def export_report(state):
    from .zhihu_scan import _write

    report = {k: v for k, v in state.items() if k not in {"candidates"}}
    report["candidate_count"] = len(state["candidates"])
    report["stage_results"] = {
        key: {field: c.get(field) for field in ("status", "stages", "result")}
        for key, c in state["candidates"].items()
    }
    _write(Path(state["directory"]) / "semantic-report.json", report)
    request = state["request"]
    markdown = (
        f"# 知乎搜索进度\n\n状态：{state['status']}\n\n已送达 {request.get('qualified', 0)}/{request['quantity']}\n\n"
        f"搜索页数：{state['metrics'].get('search_pages', 0)}；候选数：{len(state['candidates'])}\n\n"
        f"首条送达：{request.get('first_delivery_seconds', '暂无')} 秒；目标送达：{request.get('target_delivery_seconds', '暂无')} 秒\n"
    )

    report_path = Path(state["directory"]) / "semantic-report.md"
    temporary = report_path.with_suffix(".md.tmp")
    temporary.write_text(markdown, encoding="utf-8")
    temporary.replace(report_path)


async def deliver(database):
    from .zhihu_workflow import _cards

    while True:
        await asyncio.to_thread(zhihu_delivery.recover, database)
        await asyncio.to_thread(_cards, database)
        await asyncio.sleep(0.25)


async def run(database, state, shutdown):
    journal = state["stream"]
    journal["pipeline"]["active"] = {"content": 0}
    pipeline._restore(database, state, journal)
    session = None
    runtimes = []
    running = {}
    collector = None
    next_poll = 0.0
    preparation = None
    fatal = False
    sender = asyncio.create_task(deliver(database))
    started = time.monotonic()
    last_checkpoint = started
    elapsed = journal["pipeline"].get("active_seconds", 0)
    try:
        while not (shutdown and shutdown.is_set()):
            if sender.done():
                await sender
            stopping = scan.stop_requested(database, state["id"])
            rows = refresh(database, state)
            if any(v[0] == "failed" for v in rows.values()):
                state.update(
                    status="delivery_failed",
                    stop_reason="delivery_failed",
                    last_error={
                        "kind": "delivery_failed",
                        "message": "继续操作将使用原发送身份重试",
                    },
                )
                fatal = True
            if collector and collector.done():
                active = state["active"]
                try:
                    kind, value = collector.result()
                    if kind == "started":
                        active["collector_id"] = value
                    elif kind == "resumed":
                        active.pop("resume_requested", None)
                    else:
                        scan._poll(database, state, value)
                except CollectorError as exc:
                    if (
                        active["payload"]["kind"] == "detail"
                        and exc.kind == "collection"
                    ):
                        candidate = state["candidates"][active["content_key"]]
                        candidate["body_attempted"] = True
                        candidate["article"]["read_error"] = str(exc)
                        state["active"] = None
                    else:
                        raise
                collector = None
                next_poll = time.monotonic() + 0.5
                scan._save(database, state)
            for task in list(running):
                if not task.done():
                    continue
                key, runtime, attempt = running.pop(task)
                failure = pipeline._accept(
                    database, state, journal, key, attempt, task.result()
                )
                if failure:
                    fatal = True
            if not stopping and not fatal:
                register_ready(database, state)
            # A completed pass awaiting the sender lock also reserves capacity.
            rows = refresh(database, state)
            unregistered = sum(
                key not in rows
                and state["candidates"][key]["status"] in {"recommend", "uncertain"}
                for key in journal["results"]
            )
            full = (
                state["request"]["reserved"] + unregistered
                >= state["request"]["quantity"]
            )
            delivered = state["request"]["qualified"] >= state["request"]["quantity"]
            if stopping or fatal or full:
                if not running and collector is None:
                    if stopping:
                        state.update(status="stopped", stop_reason="user_stop")
                        break
                    if fatal:
                        break
                    if delivered:
                        state.update(
                            status="waiting_user", stop_reason="target_reached"
                        )
                        break
                await asyncio.sleep(0.1)
                continue
            busy_keys = {entry[0] for entry in running.values()}
            waiting = [key for key in pending(database, state) if key not in busy_keys]
            for key in list(waiting):
                candidate = state["candidates"][key]
                candidate.setdefault("stages", {})
                if key not in journal["keys"]:
                    journal["keys"].append(key)
                if candidate.get("body_attempted") and not complete_body(
                    candidate["article"]
                ):
                    scan._metric(state, "body_failed")
                    scan._save_result(
                        database,
                        state,
                        journal,
                        key,
                        {
                            "error": "body_failed",
                            "stage": "body",
                            "reason": candidate["article"].get("read_error")
                            or "正文不完整",
                        },
                    )
                    waiting.remove(key)
            if not state["active"]:
                schedule_collection(database, state, waiting)
            if state["active"] and collector is None and time.monotonic() >= next_poll:
                collector = asyncio.create_task(collect(copy.deepcopy(state["active"])))
            ready = []
            # An earlier incomplete body blocks later content evaluation.
            for key in waiting:
                if not complete_body(state["candidates"][key]["article"]):
                    break
                ready.append(key)
            if ready and preparation is None:
                config = load_required_model_config(
                    "models.toml", section="zhihu_content"
                )
                session = CodexSession()
                runtimes = [
                    CodexRuntime(config.model, config.reasoning_effort, session=session)
                ]
                preparation = asyncio.create_task(runtimes[0].prepare())
            if preparation and preparation.done():
                await preparation
                remaining = (
                    state["request"]["quantity"]
                    - state["request"]["reserved"]
                    - unregistered
                )
                if (
                    ready
                    and not running
                    and remaining > 0
                    and not scan.stop_requested(database, state["id"])
                ):
                    key = ready[0]
                    candidate = state["candidates"][key]
                    if not candidate.get("body_confirmed"):
                        candidate["body_confirmed"] = True
                        scan._metric(state, "body_success")
                    attempt = pipeline._begin(database, state, journal, key)
                    task = asyncio.create_task(
                        pipeline._invoke(runtimes[0], state, journal, key)
                    )
                    running[task] = (key, runtimes[0], attempt)
            if (
                not waiting
                and not running
                and not state["active"]
                and all(q["is_end"] for q in state["queries"])
            ):
                if state["request"]["pending"]:
                    await asyncio.sleep(0.1)
                    continue
                state.update(status="exhausted", stop_reason="results_exhausted")
                break
            journal["pipeline"]["active_seconds"] = round(
                elapsed + time.monotonic() - started, 3
            )
            if time.monotonic() - last_checkpoint >= 1:
                scan._save(database, state)
                last_checkpoint = time.monotonic()
            await asyncio.sleep(0.1)
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        if collector:
            # Collector start has a durable UUID. If shutdown interrupts waiting,
            # the same request is recovered, never a fresh browser operation.
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
        if preparation and not preparation.done():
            preparation.cancel()
            await asyncio.gather(preparation, return_exceptions=True)
        for task in running:
            if not task.done():
                task.cancel()
        if running:
            outcomes = await asyncio.gather(*running, return_exceptions=True)
            for (key, runtime, attempt), outcome in zip(
                running.values(), outcomes, strict=True
            ):
                if isinstance(outcome, BaseException):
                    outcome = (None, asyncio.CancelledError(), {})
                pipeline._accept(database, state, journal, key, attempt, outcome)
        if session:
            await session.close()
        journal["pipeline"]["active"] = {"content": 0}
        journal["pipeline"]["active_seconds"] = round(
            elapsed + time.monotonic() - started, 3
        )
        scan._save(database, state)


def step(database, state, shutdown=None):
    initialize(database, state)
    controls(database, state)
    if state["status"] not in {"running", "stopping"}:
        refresh(database, state)
        if state["status"] == "stopped":
            notify(database, state)
        scan._save(database, state)
        export_report(state)
        return state
    try:
        if not state["query_plan"] and not scan.stop_requested(database, state["id"]):
            if "search_page" not in CollectorClient.from_env().health().get(
                "capabilities", []
            ):
                raise CollectorError("protocol", "采集器需支持可恢复单页搜索")
            attempt = {"status": "running", "started_at": scan._now(), "telemetry": {}}
            state.setdefault("rewrite_attempts", []).append(attempt)
            scan._save(database, state)
            try:
                install_query_plan(
                    state, rewrite_topic(state["topic"], telemetry=attempt["telemetry"])
                )
                attempt["status"] = "completed"
            except Exception:
                attempt["status"] = "failed"
                raise
            finally:
                attempt["completed_at"] = scan._now()
                scan._save(database, state)
        state["phase"] = "stream"
        asyncio.run(run(database, state, shutdown))
    except (
        CollectorError,
        LLMError,
        ConfigError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
    ) as exc:
        kind = (
            exc.kind
            if isinstance(exc, CollectorError)
            else "model_busy"
            if isinstance(exc, CodexBusyError)
            else "model_failed"
            if isinstance(exc, LLMError)
            else "processing_failed"
        )
        state.update(
            status="waiting_login"
            if kind == "login"
            else "collector_failed"
            if isinstance(exc, CollectorError)
            else kind,
            stop_reason=kind,
            last_error={"kind": kind, "message": str(exc)[:300]},
        )
        state["errors"].append({**state["last_error"], "at": scan._now()})
    refresh(database, state)
    scan._save(database, state)
    if not (shutdown and shutdown.is_set()) and state["status"] != "running":
        notify(database, state)
    export_report(state)
    return state
