"""Semantic preferences from the owner’s current feedback, independent of RSS."""

from __future__ import annotations

import fcntl
import json
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .codex_runtime import CodexBusyError
from .database import connect, transaction
from .zhihu_store import _json, _now


def _progress(samples):
    return {
        "likes": sum(row["label"] == "like" for row in samples),
        "dislikes": sum(row["label"] == "dislike" for row in samples),
    }


def train_pending(database, force=False):
    """Background-only work. Preserve the last valid version if fitting fails."""
    path = Path(database)
    lock_path = path.with_name(path.name + ".zhihu-learning.lock")
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        try:
            return _train_pending(database, force=force)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _semantic_samples(conn, cutoff):
    result = []
    for row in conn.execute(
        """SELECT f.*,s.body,s.article_json FROM zhihu_feedback_revisions f
        JOIN zhihu_content_snapshots s ON s.snapshot_id=f.snapshot_id
        WHERE f.revision_id=(SELECT max(new.revision_id) FROM zhihu_feedback_revisions new
            WHERE new.content_key=f.content_key AND new.revision_id<=?)
        ORDER BY f.content_key""",
        (cutoff,),
    ):
        article = json.loads(row["article_json"])
        result.append(
            {
                "revision_id": row["revision_id"],
                "content_key": row["content_key"],
                "snapshot_id": row["snapshot_id"],
                "label": row["label"],
                "reason": row["reason"],
                "keywords": json.loads(row["keywords_json"]),
                "origin": row["origin"],
                "title": article.get("title", ""),
                "body": row["body"],
                "created_at": row["created_at"],
            }
        )
    return result


def snapshot(database):
    """Copy the last valid semantic version, even with one feedback category."""
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        conn.execute("BEGIN")
        cutoff = conn.execute(
            "SELECT coalesce(max(revision_id),0) FROM zhihu_feedback_revisions"
        ).fetchone()[0]
        counts = dict(
            conn.execute(
                """SELECT label,count(*) FROM zhihu_feedback_revisions f
            WHERE revision_id=(SELECT max(new.revision_id) FROM zhihu_feedback_revisions new
                WHERE new.content_key=f.content_key AND new.revision_id<=?) GROUP BY label""",
                (cutoff,),
            )
        )
        latest = conn.execute(
            "SELECT * FROM zhihu_semantic_preferences ORDER BY version DESC LIMIT 1"
        ).fetchone()
        valid = (
            conn.execute(
                "SELECT * FROM zhihu_semantic_preferences WHERE status='ready' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if cutoff
            else None
        )
        model = json.loads(valid["model_json"]) if valid else {}
        return {
            **model,
            "version": valid["version"] if valid else None,
            "ready": valid is not None,
            "preferences": model.get("preferences", []),
            "change_note": model.get("change_note", ""),
            "evidence_revision_ids": model.get("evidence_revision_ids", []),
            "progress": {
                "likes": counts.get("like", 0),
                "dislikes": counts.get("dislike", 0),
            },
            "feedback_revision": cutoff,
            "trained_revision": valid["cutoff_revision_id"] if valid else 0,
            "training_pending": cutoff > (valid["cutoff_revision_id"] if valid else 0),
            "training_error": latest["error"]
            if latest and latest["status"] != "ready"
            else "",
            "training_status": latest["status"] if latest else "untrained",
        }


def _train_pending(database, force=False):
    from .zhihu_semantics import summarize_preferences

    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        conn.execute("BEGIN")
        cutoff = conn.execute(
            "SELECT coalesce(max(revision_id),0) FROM zhihu_feedback_revisions"
        ).fetchone()[0]
        latest = conn.execute(
            "SELECT * FROM zhihu_semantic_preferences ORDER BY version DESC LIMIT 1"
        ).fetchone()
        valid = conn.execute(
            "SELECT * FROM zhihu_semantic_preferences WHERE status='ready' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not cutoff or (valid and cutoff <= valid["cutoff_revision_id"]):
            return None
        if (
            not force
            and latest
            and cutoff <= latest["cutoff_revision_id"]
            and latest["status"] != "ready"
        ):
            retry_seconds = 30 if latest["status"] == "busy" else 600
            if (
                time.time() - datetime.fromisoformat(latest["created_at"]).timestamp()
                < retry_seconds
            ):
                return None
        samples = _semantic_samples(conn, cutoff)
        previous = json.loads(valid["model_json"]) if valid else None
    started = time.monotonic()
    error = ""
    telemetry = {}
    try:
        model = summarize_preferences(samples, previous=previous, telemetry=telemetry)
        status = "ready"
    except Exception as exc:  # noqa: BLE001 - retain the previous valid preference
        status = "busy" if isinstance(exc, CodexBusyError) else "failed"
        error = f"{type(exc).__name__}: {exc}"[:1000]
        model = {
            "preferences": [],
            "change_note": "",
            "evidence_revision_ids": [],
            "error_kind": type(exc).__name__,
        }
    model.update(
        progress=_progress(samples),
        model_calls=telemetry.get("calls", 0),
        telemetry=telemetry,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    with transaction(database) as conn:
        version = conn.execute(
            "SELECT coalesce(max(version),0)+1 FROM zhihu_semantic_preferences"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO zhihu_semantic_preferences VALUES (?,?,?,?,?,?,?)",
            (version, cutoff, status, _json(model), _json(samples), error, _now()),
        )
    return {
        **model,
        "version": version,
        "cutoff_revision_id": cutoff,
        "status": status,
        "error": error,
    }
