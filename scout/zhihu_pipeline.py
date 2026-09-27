"""One durable batch coordinator, two Luna slots and one Sol consumer.

Only the coordinator mutates scan state or SQLite. Model tasks return isolated
results and telemetry; the saved stage journal and FIFO survive process restarts.
"""

from __future__ import annotations

import asyncio
import time

from . import zhihu_semantic_scan as scan
from .codex_runtime import (
    CodexBusyError,
    CodexRuntime,
    CodexSession,
    LLMError,
    ModelUnavailableError,
)
from .config import ConfigError
from .llm import load_required_model_config
from .zhihu_semantics import classify_relevance_async, evaluate_content_async

RELEVANCE_CONCURRENCY = 2
EVALUATION_CONCURRENCY = 1


def _final_result(database, state, batch, key, result):
    queue = batch["sol_queue"]
    if key in queue:
        queue.remove(key)
    scan._metric(state, result["decision"])
    scan._save_result(database, state, batch, key, result)


def _restore(database, state, batch):
    """Rebuild old journals as well as interrupted queues, without retrying failures."""
    queue = batch.setdefault("sol_queue", [])
    eligible = []
    pending = []
    for key in batch["keys"]:
        if key in batch["results"]:
            continue
        stages = state["candidates"][key].setdefault("stages", {})
        for stage in stages.values():
            for attempt in stage.get("attempts", []):
                if attempt["status"] == "running":
                    attempt.update(
                        status="interrupted", error="进程中断，未取得可复用结果"
                    )
            if stage.get("status") == "running":
                stage["status"] = "interrupted"
        failed = next(
            (
                (name, stage)
                for name, stage in stages.items()
                if stage.get("status") == "failed"
            ),
            None,
        )
        if failed:
            name, stage = failed
            scan._save_result(
                database,
                state,
                batch,
                key,
                {
                    "error": name + "_failed",
                    "stage": name,
                    "reason": stage.get("error", "模型判断失败，本篇已跳过"),
                },
            )
            continue
        relevance = stages.get("relevance", {})
        evaluation = stages.get("evaluation", {})
        if relevance.get("status") != "completed":
            pending.append(key)
        elif relevance["result"]["relevance"] == "irrelevant":
            _final_result(
                database,
                state,
                batch,
                key,
                {
                    "content_key": key,
                    "decision": "reject",
                    "reason": relevance["result"]["reason"],
                    "summary": None,
                    "rejection_stage": "relevance",
                },
            )
        elif evaluation.get("status") == "completed":
            result = evaluation["result"]
            if result["decision"] == "reject":
                result = {**result, "rejection_stage": "preference"}
            _final_result(database, state, batch, key, result)
        else:
            eligible.append(key)
    # A running Sol item remains in the persisted queue until its terminal result
    # is committed. This preserves FIFO across interruption and re-entry.
    queue[:] = list(dict.fromkeys(key for key in queue if key in eligible))
    queue.extend(key for key in eligible if key not in queue)
    scan._save(database, state)
    return pending


def _begin(database, state, batch, key, name):
    stage = state["candidates"][key]["stages"].setdefault(name, {"attempts": []})
    attempt = {
        "batch_id": batch["id"],
        "status": "running",
        "started_at": scan._now(),
        "telemetry": {},
    }
    stage.setdefault("attempts", []).append(attempt)
    stage.update(status="running", batch_id=batch["id"])
    active = batch["pipeline"]["active"]
    active[name] += 1
    peak = name + "_peak"
    batch["pipeline"][peak] = max(batch["pipeline"].get(peak, 0), active[name])
    scan._save(database, state)
    return attempt


async def _invoke(runtime, state, batch, key, name):
    telemetry = {}
    result, failure = None, None
    started = time.monotonic()
    try:
        candidate = state["candidates"][key]
        if name == "relevance":
            result = await classify_relevance_async(
                state["topic"],
                candidate["article"],
                runtime=runtime,
                telemetry=telemetry,
            )
        else:
            result = await evaluate_content_async(
                state["topic"],
                batch["preference"],
                candidate["article"],
                relevance=candidate["stages"]["relevance"]["result"],
                runtime=runtime,
                telemetry=telemetry,
            )
    except (
        LLMError,
        ConfigError,
        ValueError,
        TypeError,
        OSError,
        asyncio.CancelledError,
    ) as exc:
        failure = exc
    telemetry.setdefault("duration_seconds", round(time.monotonic() - started, 3))
    return result, failure, telemetry, time.monotonic()


def _accept(database, state, batch, key, name, attempt, outcome):
    result, failure, telemetry, _ = outcome
    stage = state["candidates"][key]["stages"][name]
    pipeline = batch["pipeline"]
    pipeline["active"][name] -= 1
    attempt.update(telemetry=telemetry, completed_at=scan._now())
    stage["telemetry"] = dict(telemetry)
    scan._metric(state, "model_calls", telemetry.get("calls", 0))
    scan._metric(state, name + "_model_calls", telemetry.get("calls", 0))
    scan._metric(state, name + "_seconds", telemetry.get("duration_seconds", 0))
    if isinstance(failure, asyncio.CancelledError):
        attempt.update(status="interrupted", error="服务退出，未取得可复用结果")
        stage["status"] = "interrupted"
        scan._save(database, state)
        return None
    if failure is not None:
        error = str(failure)[:300]
        kind = name + "_failed"
        attempt.update(status="failed", error=error)
        stage.update(status="failed", error=error)
        state["errors"].append(
            {
                "kind": kind,
                "stage": name,
                "content_key": key,
                "batch_id": batch["id"],
                "message": error,
                "at": scan._now(),
            }
        )
        scan._metric(state, kind)
        if key in batch["sol_queue"]:
            batch["sol_queue"].remove(key)
        fatal = isinstance(failure, (ModelUnavailableError, ConfigError))
        if fatal:
            # Persist the global pause with the failure, so a crash cannot turn
            # a quota/auth/transport fault into automatic failures for the rest.
            state.update(
                status="model_busy"
                if isinstance(failure, CodexBusyError)
                else "model_failed",
                last_error={"kind": kind, "stage": name, "message": error},
            )
        scan._save_result(
            database,
            state,
            batch,
            key,
            {
                "error": kind,
                "stage": name,
                "reason": error,
            },
        )
        return failure if fatal else None
    attempt["status"] = "completed"
    stage.update(status="completed", result=result, completed_at=scan._now())
    stage.pop("error", None)
    if name == "relevance" and result["relevance"] != "irrelevant":
        batch["sol_queue"].append(key)
        pipeline["queue_peak"] = max(
            pipeline.get("queue_peak", 0), len(batch["sol_queue"])
        )
        # Commit Luna's success and queue admission together, before dispatching Sol.
        scan._save(database, state)
        return None
    # Preserve stage success even if preparing the final content snapshot fails.
    scan._save(database, state)
    if name == "relevance":
        result = {
            "content_key": key,
            "decision": "reject",
            "reason": result["reason"],
            "summary": None,
            "rejection_stage": "relevance",
        }
    elif result["decision"] == "reject":
        result = {**result, "rejection_stage": "preference"}
    _final_result(database, state, batch, key, result)
    return None


async def _run(database, state, batch, shutdown):
    pipeline = batch.setdefault("pipeline", {})
    pipeline.update(
        relevance_concurrency=RELEVANCE_CONCURRENCY,
        evaluation_concurrency=EVALUATION_CONCURRENCY,
        active={"relevance": 0, "evaluation": 0},
    )
    pending = _restore(database, state, batch)
    pipeline["queue_peak"] = max(pipeline.get("queue_peak", 0), len(batch["sol_queue"]))
    if not pending and not batch["sol_queue"]:
        batch["status"] = "delivering"
        scan._save(database, state)
        return
    if scan.stop_requested(database, state["id"]) or (shutdown and shutdown.is_set()):
        return
    relevance = load_required_model_config("models.toml", section="zhihu_relevance")
    evaluation = load_required_model_config("models.toml")
    session = CodexSession()
    luna = [
        CodexRuntime(relevance.model, relevance.reasoning_effort, session=session)
        for _ in range(RELEVANCE_CONCURRENCY)
    ]
    sol = CodexRuntime(evaluation.model, evaluation.reasoning_effort, session=session)
    running = {}
    fatal = None
    started = time.monotonic()
    previous_seconds = pipeline.get("active_seconds", 0)
    try:
        # One owner initializes the shared connection before any concurrent calls.
        # Validate both models before marking an article's attempt as started.
        preparation = asyncio.create_task(_prepare_slots([*luna, sol]))
        try:
            while not preparation.done():
                if shutdown and shutdown.is_set():
                    return
                await asyncio.wait({preparation}, timeout=0.5)
            await preparation
        finally:
            if not preparation.done():
                preparation.cancel()
            await asyncio.gather(preparation, return_exceptions=True)
        while pending or batch["sol_queue"] or running:
            if shutdown and shutdown.is_set():
                break
            stopping = scan.stop_requested(database, state["id"])
            if stopping and not state.get("stop_requested"):
                scan._save(database, state)
            if session.unusable:
                if fatal is None:
                    fatal = ModelUnavailableError(
                        "共享模型会话无法继续，未完成任务已保留"
                    )
                    state.update(
                        status="model_failed",
                        last_error={"kind": "model_failed", "message": str(fatal)},
                    )
                break
            pipeline["active_seconds"] = round(
                previous_seconds + time.monotonic() - started, 3
            )
            if not stopping and fatal is None:
                busy = {entry[2] for entry in running.values()}
                work = []
                if batch["sol_queue"] and sol not in busy:
                    work.append((batch["sol_queue"][0], "evaluation", sol))
                for runtime in luna:
                    if runtime not in busy and pending:
                        work.append((pending.pop(0), "relevance", runtime))
                for key, name, runtime in work:
                    if scan.stop_requested(database, state["id"]) or (
                        shutdown and shutdown.is_set()
                    ):
                        break
                    attempt = _begin(database, state, batch, key, name)
                    task = asyncio.create_task(
                        _invoke(runtime, state, batch, key, name)
                    )
                    running[task] = (key, name, runtime, attempt)
            if not running:
                break
            done, _ = await asyncio.wait(
                running, timeout=0.5, return_when=asyncio.FIRST_COMPLETED
            )
            for task in sorted(done, key=lambda task: task.result()[3]):
                key, name, runtime, attempt = running.pop(task)
                failure = _accept(
                    database, state, batch, key, name, attempt, task.result()
                )
                if failure is not None and fatal is None:
                    fatal = failure
                    state.update(
                        status="model_busy"
                        if isinstance(failure, CodexBusyError)
                        else "model_failed",
                        last_error={
                            "kind": name + "_failed",
                            "stage": name,
                            "message": str(failure)[:300],
                        },
                    )
                    scan._save(database, state)
        if (
            fatal is None
            and not pending
            and not batch["sol_queue"]
            and not running
            and len(batch["results"]) == len(batch["keys"])
        ):
            batch["status"] = "delivering"
    finally:
        # Drain completed results and cancel only outstanding turns on shutdown.
        # The session/credential lock outlives every slot's cancellation cleanup.
        for task in running:
            if not task.done():
                task.cancel()
        try:
            if running:
                outcomes = await asyncio.gather(*running, return_exceptions=True)
                for (key, name, runtime, attempt), outcome in zip(
                    running.values(), outcomes, strict=True
                ):
                    if isinstance(outcome, BaseException):
                        outcome = (None, asyncio.CancelledError(), {}, time.monotonic())
                    _accept(database, state, batch, key, name, attempt, outcome)
        finally:
            try:
                await session.close()
            finally:
                pipeline["active"] = {"relevance": 0, "evaluation": 0}
                pipeline["active_seconds"] = round(
                    previous_seconds + time.monotonic() - started, 3
                )
                scan._save(database, state)


async def _prepare_slots(runtimes):
    for runtime in runtimes:
        await runtime.prepare()


def run_pipeline(database, state, batch, *, shutdown=None):
    asyncio.run(_run(database, state, batch, shutdown))
