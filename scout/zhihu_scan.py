"""Durable semantic scans coordinated over the collector HTTP API."""

import fcntl
import hashlib
import json
import logging
import threading
import uuid
from contextlib import closing
from pathlib import Path

from . import zhihu_delivery, zhihu_store
from .database import connect
from .locking import RunLockedError, sender_lock
from .zhihu_client import CollectorError
from .zhihu_verify import complete_body, validate_article

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
        with sender_lock(self.path):
            zhihu_delivery.initialize(self.path)
            zhihu_store.initialize(self.path)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread.is_alive():
            # Allow slot cancellation (including a late turn/start response) and
            # shared-session cleanup to finish before this daemon thread exits.
            self.thread.join(timeout=20)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                resume_pending(self.path, shutdown=self.stop)
            except Exception as exc:  # noqa: BLE001 - resume on next iteration
                logger.warning("Zhihu task recovery failed: %s", type(exc).__name__)
            self.stop.wait(5)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)


def _write(path, value):
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(_json(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def get_scan(database_path, scan_id=None):
    path = Path(database_path).resolve()
    with closing(connect(path, read_only=True)) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='zhihu_scans'"
        ).fetchone():
            return None
        if scan_id:
            from .zhihu_semantic_scan import _resolve_scan

            return _resolve_scan(conn, str(uuid.UUID(scan_id)))
        else:
            row = conn.execute(
                "SELECT state_json FROM zhihu_scans ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row[0]) if row else None


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
    names = _fetch_evidence(client, state, evidence)
    article = validate_article({**raw, "raw_files": names})
    if article["status"] == "body" and not complete_body(article):
        raise CollectorError("protocol", "完整正文缺少可靠完整性依据")
    return article


def resume_pending(database_path, *, shutdown=None):
    """Collection and delivery share recovery between CLI and listener."""
    if not Path(database_path).exists():
        return
    try:
        from .zhihu_workflow import work

        work(database_path)
        _resume_scan(database_path, shutdown=shutdown)
    finally:
        # Terminal scans still have durable links to finish after a restart.
        zhihu_delivery.recover(database_path)


def _fetch_evidence(client, state, entries):
    names = []
    for entry in entries:
        sha = entry["sha256"]
        if (
            not isinstance(sha, str)
            or len(sha) != 64
            or any(c not in "0123456789abcdef" for c in sha)
        ):
            raise CollectorError("protocol", "无效证据哈希")
        name = f"raw/{sha}.json"
        local = Path(state["directory"]) / name
        content = local.read_bytes() if local.exists() else None
        cached = content is not None and hashlib.sha256(content).hexdigest() == sha
        if not cached:
            # A process can stop midway through an older evidence write. Fetch
            # the same immutable hash again instead of treating it as new content.
            content = client.evidence(entry["path"])
        if hashlib.sha256(content).hexdigest() != sha:
            raise CollectorError("protocol", "采集证据哈希不符")
        content.decode("utf-8")
        if not cached:
            temporary = local.with_suffix(local.suffix + f".{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(content)
            temporary.replace(local)
        names.append(name)
    return names


def resume_collection(database_path, scan_id):
    from .zhihu_semantic_scan import request_control

    request_control(database_path, scan_id, "continue")


def summary(state):
    from .zhihu_semantic_scan import summary as semantic_summary

    return semantic_summary(state)


def export_report(state):
    from .zhihu_semantic_scan import export_report as semantic_report

    return semantic_report(state)


def _resume_scan(database_path, *, shutdown=None):
    from .zhihu_semantic_scan import step

    path = Path(database_path)
    with path.with_suffix(path.suffix + ".zhihu-scan.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with closing(connect(path, read_only=True)) as conn:
            row = conn.execute("""SELECT id FROM zhihu_scans s
                WHERE status IN ('running','stopping','waiting_preference')
                OR EXISTS (SELECT 1 FROM zhihu_jobs j WHERE j.kind='semantic_control'
                    AND j.status='pending' AND json_extract(j.payload_json,'$.scan_id')=s.id)
                ORDER BY created_at LIMIT 1""").fetchone()
        if row:
            try:
                step(path, row[0], shutdown=shutdown)
            except RunLockedError:
                return
