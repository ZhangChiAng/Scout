"""One durable content evaluation per article; only the coordinator writes state."""

from __future__ import annotations

import asyncio
import copy
import time

from . import zhihu_semantic_scan as scan
from .codex_runtime import CodexBusyError, LLMError, ModelUnavailableError
from .config import ConfigError
from .zhihu_semantics import evaluate_content_async

CONTENT_CONCURRENCY = 1


def _final_result(database, state, journal, key, result):
    scan._metric(state, result["decision"])
    scan._save_result(database, state, journal, key, result)


def _restore(database, state, journal):
    """Reuse completed results; retry only interrupted calls, never ordinary failures."""
    for key in journal["keys"]:
        if key in journal["results"]:
            continue
        stage = state["candidates"][key].get("stages", {}).get("content", {})
        for attempt in stage.get("attempts", []):
            if attempt["status"] == "running":
                attempt.update(status="interrupted", error="进程中断，未取得可复用结果")
        if stage.get("status") == "running":
            stage["status"] = "interrupted"
        if stage.get("status") == "completed":
            _final_result(database, state, journal, key, stage["result"])
        elif stage.get("status") == "failed":
            scan._save_result(
                database,
                state,
                journal,
                key,
                {
                    "error": "content_failed",
                    "stage": "content",
                    "reason": stage.get("error", "内容评价失败，本篇已跳过"),
                },
            )
    scan._save(database, state)


def _begin(database, state, journal, key):
    stage = (
        state["candidates"][key]
        .setdefault("stages", {})
        .setdefault("content", {"attempts": []})
    )
    preference = copy.deepcopy(journal["preference"])
    attempt = {
        "request_id": state["request"]["id"],
        "status": "running",
        "started_at": scan._now(),
        "telemetry": {},
        "preference_version": preference.get("version"),
    }
    stage["attempts"].append(attempt)
    stage.update(status="running", preference=preference)
    journal["pipeline"]["active"]["content"] = 1
    journal["pipeline"]["content_peak"] = 1
    scan._save(database, state)
    return attempt


async def _invoke(runtime, state, journal, key):
    telemetry = {}
    result, failure = None, None
    started = time.monotonic()
    try:
        candidate = state["candidates"][key]
        result = await evaluate_content_async(
            state["topic"],
            candidate["stages"]["content"]["preference"],
            candidate["article"],
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
    return result, failure, telemetry


def _accept(database, state, journal, key, attempt, outcome):
    result, failure, telemetry = outcome
    stage = state["candidates"][key]["stages"]["content"]
    journal["pipeline"]["active"]["content"] = 0
    attempt.update(telemetry=telemetry, completed_at=scan._now())
    stage["telemetry"] = dict(telemetry)
    scan._metric(state, "model_calls", telemetry.get("calls", 0))
    scan._metric(state, "content_model_calls", telemetry.get("calls", 0))
    scan._metric(state, "content_seconds", telemetry.get("duration_seconds", 0))
    if isinstance(failure, asyncio.CancelledError):
        attempt.update(status="interrupted", error="服务退出，未取得可复用结果")
        stage["status"] = "interrupted"
        scan._save(database, state)
        return None
    if failure is not None:
        error = str(failure)[:300]
        fatal = isinstance(failure, (ModelUnavailableError, ConfigError))
        attempt.update(status="failed", error=error)
        stage.update(status="failed", error=error, retry_on_continue=fatal)
        state["errors"].append(
            {
                "kind": "content_failed",
                "stage": "content",
                "content_key": key,
                "request_id": state["request"]["id"],
                "message": error,
                "at": scan._now(),
            }
        )
        scan._metric(state, "content_failed")
        if fatal:
            state.update(
                status="model_busy"
                if isinstance(failure, CodexBusyError)
                else "model_failed",
                last_error={"kind": "content_failed", "message": error},
            )
        scan._save_result(
            database,
            state,
            journal,
            key,
            {
                "error": "content_failed",
                "stage": "content",
                "reason": error,
            },
        )
        return failure if fatal else None
    attempt["status"] = "completed"
    stage.update(status="completed", result=result, completed_at=scan._now())
    stage.pop("error", None)
    scan._save(database, state)
    _final_result(database, state, journal, key, result)
    return None
