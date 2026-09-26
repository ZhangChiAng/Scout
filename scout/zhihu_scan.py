"""Durable single scans, coordinated exclusively over the collector HTTP API."""

import fcntl
import hashlib
import json
import logging
import sqlite3
import threading
import time
import tomllib
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError
from .rules import Rules
from .zhihu_client import CollectorClient, CollectorError
from .zhihu_verify import evaluate_article, render_report, validate_article

TERMINAL = {"completed", "partial_failed", "failed"}
logger = logging.getLogger(__name__)


class ZhihuScanWorker:
    """Resume persisted scans while Cookie login is managed by the collector."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="scout-zhihu-scan", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                resume_pending(self.path)
            except Exception as exc:  # noqa: BLE001 - resume on next iteration
                logger.warning("Zhihu task recovery failed: %s", type(exc).__name__)
            self.stop.wait(5)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)


def _write(path, value):
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(_json(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_scan_config(path):
    with Path(path).open("rb") as stream:
        raw = tomllib.load(stream)
    if set(raw) != {"scan", "rules"}:
        raise ConfigError("知乎配置只允许 [scan] 和 [rules]")
    rules = Rules.load(raw["rules"])
    scan = raw["scan"]
    if not isinstance(scan, dict) or set(scan) != {
        "queries",
        "max_unique",
        "max_results",
        "detail_batch_size",
    }:
        raise ConfigError("scan 需要 queries/max_unique/max_results/detail_batch_size")
    for name, ceiling in (
        ("max_unique", 200),
        ("max_results", 5),
        ("detail_batch_size", 20),
    ):
        if type(scan[name]) is not int or not 1 <= scan[name] <= ceiling:
            raise ConfigError(f"scan.{name} 必须在 1..{ceiling} 内")
    if not isinstance(scan["queries"], list) or not scan["queries"]:
        raise ConfigError("scan.queries 不能为空")
    for query in scan["queries"]:
        if not isinstance(query, dict) or set(query) != {"query", "sort", "max_pages"}:
            raise ConfigError("每个查询需要 query/sort/max_pages")
        if not isinstance(query["query"], str) or not query["query"].strip():
            raise ConfigError("query 不能为空")
        if query["sort"] not in {"general", "latest"}:
            raise ConfigError("sort 必须为 general/latest")
        if type(query["max_pages"]) is not int or not 1 <= query["max_pages"] <= 3:
            raise ConfigError("max_pages 必须在 1..3 内")
    return {"scan": scan, "rules": rules.snapshot()}


def _connect(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def initialize(path):
    with closing(_connect(path)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS zhihu_scans (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            status TEXT NOT NULL, state_json TEXT NOT NULL
        )""")


def create_scan(database_path, config_path, request_uuid=None):
    config = load_scan_config(config_path)  # Before any HTTP, including health.
    scan_id = str(uuid.UUID(request_uuid)) if request_uuid else str(uuid.uuid4())
    initialize(database_path)
    with closing(_connect(database_path)) as conn:
        row = conn.execute(
            "SELECT state_json FROM zhihu_scans WHERE id=?", (scan_id,)
        ).fetchone()
        if row:
            return json.loads(row[0])
        directory = Path(database_path).resolve().parent / "zhihu-runs" / scan_id
        (directory / "raw").mkdir(parents=True, mode=0o700, exist_ok=True)
        state = {
            "id": scan_id,
            "created_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "config": config,
            "directory": str(directory),
            "query_index": 0,
            "candidates": [],
            "detail_index": 0,
            "records": [],
            "coverage": [],
            "operations": [],
            "active": None,
            "errors": [],
            "stop_reason": "",
            "last_error": None,
        }
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO zhihu_scans VALUES (?,?,?,?)",
                (scan_id, state["created_at"], state["status"], _json(state)),
            )
            state = json.loads(
                conn.execute(
                    "SELECT state_json FROM zhihu_scans WHERE id=?", (scan_id,)
                ).fetchone()[0]
            )
        _write(directory / "scan-config.json", state["config"])
        return state


def get_scan(database_path, scan_id=None):
    path = Path(database_path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='zhihu_scans'"
        ).fetchone():
            return None
        if scan_id:
            row = conn.execute(
                "SELECT state_json FROM zhihu_scans WHERE id=?",
                (str(uuid.UUID(scan_id)),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT state_json FROM zhihu_scans ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row[0]) if row else None


def _save(database_path, state):
    with closing(_connect(database_path)) as conn, conn:
        conn.execute(
            "UPDATE zhihu_scans SET status=?,state_json=? WHERE id=?",
            (state["status"], _json(state), state["id"]),
        )


def apply_reviews(database_path, scan_id, entries):
    """Persist factual context review; rejected contexts reopen a bounded scan."""
    with Path(str(database_path) + ".zhihu-scan.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = get_scan(database_path, scan_id)
        if state is None:
            raise ConfigError("找不到待核对扫描")
        by_key = {record["article_key"]: record for record in state["records"]}
        for key, review in entries.items():
            article = by_key.get(key)
            if article is None or article["status"] != "body":
                raise ConfigError("语境核对必须对应本次完整正文")
            if hashlib.sha256(article["body"].encode()).hexdigest() != review.get(
                "body_sha256"
            ):
                raise ConfigError("语境核对与正文哈希不符")
            if (
                type(review.get("gpt6_is_model")) is not bool
                or not isinstance(review.get("notes"), str)
                or not review["notes"].strip()
            ):
                raise ConfigError("语境核对需要明确布尔结论和说明")
        reviews = {**state.get("semantic_reviews", {}), **entries}
        state["semantic_reviews"] = reviews
        state["semantic_exclusions"] = [
            key for key, review in reviews.items() if review["gpt6_is_model"] is False
        ]
        if (
            state["stop_reason"] == "max_results"
            and summary(state)["eligible"] < state["config"]["scan"]["max_results"]
        ):
            state["status"], state["stop_reason"] = "running", ""
        _save(database_path, state)
        export_report(state)
        return state


def summary(state):
    rules = Rules.load(state["config"]["rules"])
    results = [evaluate_article(record, rules) for record in state["records"]]
    return {
        "id": state["id"],
        "status": state["status"],
        "directory": state["directory"],
        "unique_candidates": len(state["candidates"]),
        "details_checked": len(results),
        "body_success": sum(row["article"]["status"] == "body" for row in results),
        "eligible": sum(
            row["eligible"]
            and row["article"]["article_key"]
            not in state.get("semantic_exclusions", [])
            for row in results
        ),
        "coverage": state["coverage"],
        "stop_reason": state["stop_reason"],
        "collector_run_id": (state["active"] or {}).get("collector_id"),
        "active_coverage": (state["active"] or {}).get("remote_coverage", []),
        "login_status": (state["active"] or {}).get("login_status"),
        "login_error": (state["active"] or {}).get("login_error"),
        "errors": state["errors"],
        "last_error": state["last_error"],
        "semantic_exclusions": state.get("semantic_exclusions", []),
    }


def export_report(state, rules=None):
    directory = Path(state["directory"])
    rules = rules or Rules.load(state["config"]["rules"])
    records = state["records"]
    collection = {
        "schema_version": 1,
        "queries": [q["query"] for q in state["config"]["scan"]["queries"]],
        "records": records,
        "coverage": state["coverage"],
        "status": state["status"],
        "errors": state["errors"],
    }
    _write(directory / "collection.json", collection)
    results = [evaluate_article(record, rules) for record in records]
    complete = [r for r in results if r["article"]["status"] == "body"]
    hashes = {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for record in records
        for name in record["raw_files"]
    }
    report = {
        "schema_version": 1,
        "scored_at": datetime.now(UTC).isoformat(),
        "queries": collection["queries"],
        "input_file": "collection.json",
        "input_sha256": hashlib.sha256(
            (directory / "collection.json").read_bytes()
        ).hexdigest(),
        "rules": rules.snapshot(),
        "rules_sha256": hashlib.sha256(_json(rules.snapshot()).encode()).hexdigest(),
        "raw_sha256": hashes,
        "results": results,
        "coverage": state["coverage"],
        "collection_status": state["status"],
        "errors": state["errors"],
        "summary": {
            "candidates": len(state["candidates"]),
            "details_checked": len(results),
            **{
                status: sum(r["article"]["status"] == status for r in results)
                for status in ("body", "partial_body", "summary_only", "failed")
            },
            "read_failures": sum(bool(r["article"]["read_error"]) for r in results),
            "body_recalled": sum(r["recalled"] for r in complete),
            "body_no_match": sum(not r["recalled"] for r in complete),
            "retained": sum(r["retained"] for r in results),
        },
        "scope": "仅覆盖所列查询、实际页数与已请求详情。失败和未完成采集不能算零命中。规则只匹配文本，发送前另核对 GPT-6 模型语境。",
    }
    _write(directory / "scores.json", report)
    (directory / "scores.md").write_text(
        render_report(report)
        + "\n\n采集状态与覆盖：\n\n```json\n"
        + _json(summary(state))
        + "\n```\n",
        encoding="utf-8",
    )
    return report


def _record(client, state, raw):
    if not isinstance(raw, dict):
        raise CollectorError("protocol", "采集记录不是对象")

    # Cookies belong to the collector. Reject accidental secret fields at this boundary.
    def secrets(value):
        if isinstance(value, dict):
            return any(
                str(k).lower() in {"cookie", "cookies", "authorization", "access_token"}
                or secrets(v)
                for k, v in value.items()
            )
        return isinstance(value, list) and any(secrets(v) for v in value)

    if secrets(raw):
        raise CollectorError("protocol", "采集器记录包含禁止的会话字段")
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise CollectorError("protocol", "缺少采集证据引用")
    names = []
    for entry in evidence:
        sha = entry["sha256"]
        if (
            not isinstance(sha, str)
            or len(sha) != 64
            or any(c not in "0123456789abcdef" for c in sha)
        ):
            raise CollectorError("protocol", "无效证据哈希")
        name = f"raw/{sha}.json"
        local = Path(state["directory"]) / name
        content = (
            local.read_bytes() if local.exists() else client.evidence(entry["path"])
        )
        if hashlib.sha256(content).hexdigest() != sha:
            raise CollectorError("protocol", "采集证据哈希不符")
        content.decode("utf-8")
        local.write_bytes(content)
        names.append(name)
    article = validate_article({**raw, "raw_files": names})
    if article["status"] == "body":
        basis = article.get("completeness")
        if (
            not isinstance(basis, dict)
            or not basis.get("basis")
            or basis.get("verified") is not True
            or basis.get("target_id") != article["content_id"]
            or basis.get("target_type") != article["content_type"]
            or basis.get("target_id_verified") is not True
            or basis.get("content_field_present") is not True
            or basis.get("limitations") != []
        ):
            raise CollectorError("protocol", "完整正文缺少可靠完整性依据")
    return article


def _next(state):
    scan = state["config"]["scan"]
    if summary(state)["eligible"] >= scan["max_results"]:
        state["stop_reason"] = "max_results"
        return None
    offset = state["detail_index"]
    if offset < len(state["candidates"]):
        batch = min(
            scan["detail_batch_size"], scan["max_results"] - summary(state)["eligible"]
        )
        items = state["candidates"][offset : offset + batch]
        label = f"detail:{offset}:{len(items)}"
        payload = {"kind": "detail", "items": items}
    elif len(state["candidates"]) >= scan["max_unique"]:
        state["stop_reason"] = "max_unique"
        return None
    elif state["query_index"] >= len(scan["queries"]):
        state["stop_reason"] = "queries_exhausted"
        return None
    else:
        label = f"search:{state['query_index']}"
        payload = {"kind": "search", **scan["queries"][state["query_index"]]}
    payload["request_uuid"] = str(uuid.uuid5(uuid.UUID(state["id"]), label))
    return {"payload": payload, "collector_id": None, "label": label}


def _tick(database_path, state, client):
    if state["status"] in TERMINAL:
        return
    if state["active"] is None:
        state["active"] = _next(state)
        if state["active"] is None:
            state["status"] = (
                "partial_failed"
                if state["errors"]
                or any(r["status"] != "body" for r in state["records"])
                else "completed"
            )
            _save(database_path, state)
            export_report(state)
            return
        # Persist request UUID before POST. Retrying after a crash returns the same run.
        _save(database_path, state)
    active = state["active"]
    if active["collector_id"] is None:
        result = client.start(active["payload"])
        active["collector_id"] = str(uuid.UUID(result["id"]))
        _save(database_path, state)
        return
    result = client.run(active["collector_id"])
    status = result.get("status")
    active["remote_status"] = status
    active["remote_coverage"] = result.get("coverage", [])
    active["remote_error"] = result.get("error")
    if status != "waiting_login" and not result.get("login_required"):
        active.pop("login_status", None)
        active.pop("login_error", None)
    active["evidence_files"] = []
    for evidence in result.get("evidence", []):
        sha = evidence["sha256"]
        if (
            not isinstance(sha, str)
            or len(sha) != 64
            or any(c not in "0123456789abcdef" for c in sha)
        ):
            raise CollectorError("protocol", "任务证据哈希无效")
        local = Path(state["directory"]) / "raw" / (sha + ".json")
        content = (
            local.read_bytes() if local.exists() else client.evidence(evidence["path"])
        )
        if hashlib.sha256(content).hexdigest() != sha:
            raise CollectorError("protocol", "任务证据哈希不符")
        content.decode("utf-8")
        local.write_bytes(content)
        active["evidence_files"].append(str(local.relative_to(state["directory"])))
    _save(database_path, state)
    if (
        status == "waiting_login"
        or (result.get("error") or {}).get("kind") == "verification_required"
    ):
        login = client.login_status()
        active["login_status"] = login.get("status")
        active["login_error"] = login.get("error")
        # Cookie import verifies the account and resumes the original collector run.
        # Keep polling that same run while its durable worker wakes up.
        state["status"] = (
            "running" if login.get("status") == "verified" else "waiting_login"
        )
        _save(database_path, state)
        export_report(state)
        return
    if status in {"queued", "running"}:
        state["status"] = "running"
        state["last_error"] = None
        _save(database_path, state)
        return
    if status not in TERMINAL:
        raise CollectorError("protocol", "未知采集任务状态")
    response = client.records(active["collector_id"])
    if not isinstance(response.get("records"), list):
        raise CollectorError("protocol", "缺少 records 数组")
    coverage = response.get("coverage", result.get("coverage", []))
    if not isinstance(coverage, list):
        raise CollectorError("protocol", "coverage 必须是数组")
    operation = {
        "id": active["collector_id"],
        "kind": active["payload"]["kind"],
        "status": status,
        "coverage": coverage,
        "error": result.get("error"),
    }
    state["operations"].append(operation)
    if status != "completed":
        state["errors"].append(operation)
    if active["payload"]["kind"] == "search":
        query_index = state["query_index"] + 1
        seen = {(a["content_type"], a["content_id"]) for a in state["candidates"]}
        for raw in response["records"]:
            key = (raw.get("content_type"), raw.get("content_id"))
            if (
                key in seen
                or len(state["candidates"]) >= state["config"]["scan"]["max_unique"]
            ):
                continue
            article = _record(client, state, raw)
            article["first_discovery"]["query_index"] = query_index
            state["candidates"].append(article)
            seen.add(key)
        state["coverage"].append(
            {
                "query_index": query_index,
                **state["config"]["scan"]["queries"][query_index - 1],
                "collector": coverage,
                "status": status,
                "unique_candidates": sum(
                    a["first_discovery"]["query_index"] == query_index
                    for a in state["candidates"]
                ),
                "body_success": 0,
                "detail_failures": [],
                "error": result.get("error"),
            }
        )
        state["query_index"] += 1
    else:
        by_key = {
            (r.get("content_type"), r.get("content_id")): r for r in response["records"]
        }
        expected = {
            (r["content_type"], r["content_id"]) for r in active["payload"]["items"]
        }
        if len(by_key) != len(response["records"]) or set(by_key) != expected:
            raise CollectorError("protocol", "详情返回 ID 集合与请求不符或重复")
        for candidate in active["payload"]["items"]:
            key = (candidate["content_type"], candidate["content_id"])
            if key not in by_key:
                raise CollectorError("protocol", "详情响应遗漏已请求内容 ID")
            article = _record(client, state, by_key[key])
            article["first_discovery"] = candidate["first_discovery"]
            state["records"].append(article)
            row = state["coverage"][candidate["first_discovery"]["query_index"] - 1]
            if article["status"] == "body":
                row["body_success"] += 1
            else:
                row["detail_failures"].append(
                    {
                        "article_key": article["article_key"],
                        "status": article["status"],
                        "reason": article["read_error"],
                    }
                )
        state["detail_index"] += len(active["payload"]["items"])
    state["active"] = None
    state["status"] = "running"
    state["last_error"] = None
    state["retry_count"] = 0
    _save(database_path, state)
    export_report(state)


def resume_pending(database_path):
    """One finite step; the existing listener calls this after process restarts."""
    path = Path(database_path)
    if not path.exists():
        return
    lock = path.with_suffix(path.suffix + ".zhihu-scan.lock")
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with closing(_connect(path)) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='zhihu_scans'"
            ).fetchone():
                return
            row = conn.execute(
                "SELECT state_json FROM zhihu_scans WHERE status IN ('running','waiting_login') ORDER BY created_at LIMIT 1"
            ).fetchone()
        if not row:
            return
        state = json.loads(row[0])
        if time.time() < state.get("next_retry_at", 0):
            return
        try:
            _tick(path, state, CollectorClient.from_env())
        except (
            CollectorError,
            ConfigError,
            ValueError,
            KeyError,
            TypeError,
            OSError,
        ) as exc:
            # Never persist response bodies, credentials or unbounded traceback text.
            # Discard partially applied in-memory results; retry the durable operation.
            state = get_scan(path, state["id"])
            kind = getattr(exc, "kind", "validation")
            state["retry_count"] = state.get("retry_count", 0) + 1
            state["last_error"] = {
                "kind": kind,
                "message": str(exc)[:300],
                "attempt": state["retry_count"],
            }
            state["next_retry_at"] = time.time() + min(
                30, 5 * 2 ** min(state["retry_count"], 3)
            )
            if kind not in {"network", "http_error"} or state["retry_count"] >= 5:
                state["errors"].append(
                    {**state["last_error"], "operation": state["active"]}
                )
                state["coverage"].append(
                    {
                        "status": "unverified",
                        "reason": "采集响应或正文校验失败，不能确认覆盖或按零命中统计",
                        "operation": state["active"],
                    }
                )
                state["status"] = "partial_failed" if state["records"] else "failed"
                state["stop_reason"] = "collector_error"
            _save(path, state)
            export_report(state)
