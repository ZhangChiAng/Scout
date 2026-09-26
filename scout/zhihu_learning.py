"""Versioned literal-keyword learning, independent of RSS model preferences.

The objective is class-balanced mean logistic loss plus 0.1 / 2 * ||w||²,
with w <= 0 and an unregularized training intercept. Serving uses only the
keyword sum; the empirical threshold is chosen separately with score zero
always accepted. Only the latest label for each stable content identity trains.
"""

from __future__ import annotations

import fcntl
import json
import math
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from .database import connect, transaction
from .zhihu_store import _json, _now

L2 = 0.1


def _samples(conn, cutoff):
    return [
        {**dict(row), "keywords": json.loads(row["keywords_json"])}
        for row in conn.execute(
            """SELECT f.*,s.body FROM zhihu_feedback_revisions f
            JOIN zhihu_content_snapshots s ON s.snapshot_id=f.snapshot_id
            WHERE f.revision_id=(SELECT max(new.revision_id)
                FROM zhihu_feedback_revisions new
                WHERE new.content_key=f.content_key AND new.revision_id<=?)
            ORDER BY f.content_key""",
            (cutoff,),
        )
    ]


def _progress(samples):
    return {
        "likes": sum(row["label"] == "like" for row in samples),
        "dislikes": sum(row["label"] == "dislike" for row in samples),
    }


def snapshot(database):
    """Copy the effective version for a scan; later feedback never mutates it."""
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
        progress = {
            "likes": counts.get("like", 0),
            "dislikes": counts.get("dislike", 0),
        }
        latest = conn.execute(
            "SELECT * FROM zhihu_learning_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        valid = conn.execute(
            "SELECT * FROM zhihu_learning_versions WHERE status='ready' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        both = bool(progress["likes"] and progress["dislikes"])
        ready = both and valid is not None
        model = json.loads(valid["model_json"]) if ready else {}
        attempted = latest["cutoff_revision_id"] if latest else 0
        return {
            **model,
            "version": valid["version"] if ready else None,
            "ready": ready,
            "weights": model.get("weights", {}),
            "threshold": model.get("threshold", 0.0),
            "progress": progress,
            "fit": model.get("fit", {}),
            "feedback_revision": cutoff,
            "trained_revision": valid["cutoff_revision_id"] if ready else 0,
            "training_pending": cutoff > attempted,
            "training_error": latest["error"]
            if latest and latest["status"] == "failed"
            else "",
        }


def score(body, model):
    folded = body.casefold()
    return math.fsum(
        weight
        for keyword, weight in model.get("weights", {}).items()
        if keyword in folded
    )


def evaluate(body, model):
    value = score(body, model)
    threshold = min(0.0, model.get("threshold", 0.0))
    return {
        "score": value,
        "threshold": threshold,
        "retained": value >= threshold,
        "model_version": model.get("version"),
    }


def _sigmoid(value):
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    result = math.exp(value)
    return result / (1.0 + result)


def _gradient(weights, intercept, rows, l2):
    gradient = [l2 * value for value in weights]
    intercept_gradient = 0.0
    for indices, label, mass in rows:
        z = intercept + math.fsum(weights[index] for index in indices)
        error = mass * (_sigmoid(z) - label)
        intercept_gradient += error
        for index in indices:
            gradient[index] += error
    return gradient, intercept_gradient


def _optimize(rows, dimensions, l2):
    """Projected accelerated gradient with a global logistic Hessian bound."""
    weights = [0.0] * dimensions
    accelerated = weights.copy()
    intercept = accelerated_intercept = 0.0
    momentum = 1.0
    # trace bounds the maximum eigenvalue of sum(mass * x x^T); the
    # extra coordinate is the intercept. This makes each step globally safe.
    lipschitz = (
        0.25 * math.fsum(mass * (1 + len(indices)) for indices, _, mass in rows) + l2
    )
    step = 1.0 / lipschitz
    residual = math.inf
    for iteration in range(1, 10001):
        gradient, bias_gradient = _gradient(
            accelerated, accelerated_intercept, rows, l2
        )
        updated = [
            min(0.0, value - step * grad) for value, grad in zip(accelerated, gradient)
        ]
        updated_intercept = accelerated_intercept - step * bias_gradient
        if iteration % 10 == 0:
            actual_gradient, actual_bias = _gradient(
                updated, updated_intercept, rows, l2
            )
            residual = max(
                abs(actual_bias),
                max(
                    (
                        abs(value - min(0.0, value - grad))
                        for value, grad in zip(updated, actual_gradient)
                    ),
                    default=0.0,
                ),
            )
            if residual <= 1e-7:
                return updated, updated_intercept, iteration, residual
        next_momentum = (1.0 + math.sqrt(1.0 + 4.0 * momentum * momentum)) / 2.0
        ratio = (momentum - 1.0) / next_momentum
        accelerated = [new + ratio * (new - old) for new, old in zip(updated, weights)]
        accelerated_intercept = updated_intercept + ratio * (
            updated_intercept - intercept
        )
        weights, intercept, momentum = updated, updated_intercept, next_momentum
    raise ValueError(f"关键词学习未收敛，约束梯度残差 {residual:.6g}")


def _threshold(scores, labels):
    likes, dislikes = labels.count(1), labels.count(0)
    # Passing is inclusive: each observed score and zero enumerate every
    # possible separation permitted by threshold <= 0 and mandatory zero pass.
    best = None
    for threshold in sorted({0.0, *scores}):
        true_like = sum(
            label == 1 and value >= threshold for value, label in zip(scores, labels)
        )
        false_like = sum(
            label == 0 and value >= threshold for value, label in zip(scores, labels)
        )
        true_dislike = dislikes - false_like
        # Integer numerators avoid floating-point ties in balanced accuracy.
        objective = true_like * dislikes + true_dislike * likes
        choice = (objective, -false_like, threshold)
        if best is None or choice > best[0]:
            best = (
                choice,
                {
                    "true_like": true_like,
                    "false_like": false_like,
                    "true_dislike": true_dislike,
                    "false_dislike": likes - true_like,
                    "balanced_accuracy": 0.5
                    * (true_like / likes + true_dislike / dislikes),
                },
            )
    return best[0][2], best[1]


def fit(samples, l2=L2):
    """Fit a deterministic model using the captured effective feedback set."""
    if not math.isfinite(l2) or l2 <= 0:
        raise ValueError("L2 系数必须大于零")
    progress = _progress(samples)
    if not progress["likes"] or not progress["dislikes"]:
        return {
            "ready": False,
            "weights": {},
            "threshold": 0.0,
            "progress": progress,
            "fit": {"reason": "collecting_both_labels"},
            "l2": l2,
        }
    vocabulary = sorted(
        {
            keyword
            for row in samples
            if row["label"] == "dislike"
            for keyword in row["keywords"]
        }
    )
    rows, feature_groups = [], defaultdict(list)
    for row in samples:
        body = row["body"].casefold()
        indices = tuple(
            index for index, keyword in enumerate(vocabulary) if keyword in body
        )
        label = int(row["label"] == "like")
        mass = 0.5 / progress["likes" if label else "dislikes"]
        rows.append((indices, label, mass))
        feature_groups[indices].append(row)
    weights, intercept, iterations, residual = _optimize(rows, len(vocabulary), l2)
    scores = [math.fsum(weights[index] for index in indices) for indices, _, _ in rows]
    labels = [label for _, label, _ in rows]
    threshold, metrics = _threshold(scores, labels)
    conflicts = [
        {
            "keywords": [vocabulary[index] for index in indices],
            "content_keys": [row["content_key"] for row in group],
            "revision_ids": [row["revision_id"] for row in group],
            "likes": sum(row["label"] == "like" for row in group),
            "dislikes": sum(row["label"] == "dislike" for row in group),
        }
        for indices, group in feature_groups.items()
        if len({row["label"] for row in group}) > 1
    ]
    loss = 0.5 * l2 * math.fsum(value * value for value in weights)
    for (indices, label, mass), value in zip(rows, scores):
        z = intercept + value
        loss += mass * (max(z, 0.0) - label * z + math.log1p(math.exp(-abs(z))))
    return {
        "ready": True,
        "weights": dict(zip(vocabulary, weights)),
        "threshold": min(0.0, threshold),
        "progress": progress,
        "l2": l2,
        "training_intercept": intercept,
        "fit": {
            **metrics,
            "iterations": iterations,
            "objective": loss,
            "projected_gradient": residual,
            "converged": True,
            "sample_count": len(samples),
            "feature_count": len(vocabulary),
            "conflicts": conflicts,
            "conflict_count": len(conflicts),
            "samples": [
                {
                    "content_key": row["content_key"],
                    "revision_id": row["revision_id"],
                    "label": row["label"],
                    "score": value,
                    "retained": value >= threshold,
                }
                for row, value in zip(samples, scores)
            ],
        },
    }


def train_pending(database):
    """Background-only work. Preserve the last valid version if fitting fails."""
    path = Path(database)
    lock_path = path.with_name(path.name + ".zhihu-learning.lock")
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        try:
            return _train_pending(database)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _train_pending(database):
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        conn.execute("BEGIN")
        cutoff = conn.execute(
            "SELECT coalesce(max(revision_id),0) FROM zhihu_feedback_revisions"
        ).fetchone()[0]
        previous = conn.execute(
            "SELECT coalesce(max(cutoff_revision_id),0) FROM zhihu_learning_versions"
        ).fetchone()[0]
        if cutoff <= previous:
            return None
        samples = _samples(conn, cutoff)
    error = ""
    try:
        model = fit(samples)
        status = "ready" if model["ready"] else "calibrating"
    except Exception as exc:  # noqa: BLE001 - keep the previous effective model
        error = f"{type(exc).__name__}: {exc}"[:1000]
        status = "failed"
        model = {
            "ready": False,
            "progress": _progress(samples),
            "weights": {},
            "threshold": 0.0,
            "fit": {},
        }
    with transaction(database) as conn:
        version = conn.execute(
            "SELECT coalesce(max(version),0)+1 FROM zhihu_learning_versions"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO zhihu_learning_versions VALUES (?,?,?,?,?,?)",
            (version, cutoff, status, _json(model), error, _now()),
        )
    return {
        **model,
        "version": version,
        "cutoff_revision_id": cutoff,
        "status": status,
        "error": error,
    }
