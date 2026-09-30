"""One-time offline upgrade. Stop Scout services before invoking this module."""

import fcntl
import json
import os
import uuid
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path

from scout.config import load_dotenv
from scout.database import connect, transaction
from scout.locking import sender_lock

MARKER = "zhihu_content_v1_upgrade"


def upgrade(database):
    path = Path(database).resolve()
    with ExitStack() as stack:
        for suffix in (
            ".listener.lock",
            ".zhihu-workflow.lock",
            ".zhihu-scan.lock",
            ".zhihu-learning.lock",
        ):
            handle = stack.enter_context(path.with_name(path.name + suffix).open("a+b"))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stack.enter_context(sender_lock(path))
        with closing(connect(path, read_only=True)) as source:
            if source.execute(
                "SELECT 1 FROM zhihu_settings WHERE key=?", (MARKER,)
            ).fetchone():
                return {"already_upgraded": True}
            if source.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Source integrity check failed")
            backup = (
                path.parent
                / "backups"
                / ("before-content-v1-" + uuid.uuid4().hex + ".sqlite3")
            )
            backup.parent.mkdir(exist_ok=True, mode=0o700)
            with closing(connect(backup)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchall() != [
                    ("ok",)
                ]:
                    raise RuntimeError("Backup integrity check failed")
            backup.chmod(0o600)
        stamp = datetime.now(UTC).isoformat()
        with transaction(path) as conn:
            preserved_tables = (
                "zhihu_content_snapshots",
                "zhihu_topics",
                "scout_owner",
                "zhihu_feedback_revisions",
                "zhihu_semantic_preferences",
            )
            before = {
                name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in preserved_tables
            }
            delivered = conn.execute(
                "SELECT count(*) FROM zhihu_link_deliveries WHERE status='delivered'"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT id,state_json FROM zhihu_scans WHERE coalesce(json_extract(state_json,'$.schema_version'),0) != 5"
            ).fetchall()
            for scan_id, raw in rows:
                state = json.loads(raw)
                # Historical stage journals remain opaque; never convert or resume them.
                state.update(
                    status="stopped",
                    stop_reason=MARKER,
                    stop_requested=True,
                    retired_at=stamp,
                )
                conn.execute(
                    "UPDATE zhihu_scans SET status='stopped',state_json=? WHERE id=?",
                    (json.dumps(state, ensure_ascii=False), scan_id),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO zhihu_settings VALUES (?, 'true')",
                    ("semantic_stop:" + scan_id,),
                )
                conn.execute(
                    "UPDATE zhihu_link_deliveries SET status='failed',attempts=3,last_error=? WHERE scan_id=? AND status!='delivered'",
                    (MARKER, scan_id),
                )
            jobs = conn.execute(
                "UPDATE zhihu_jobs SET status='cancelled',last_error=?,updated_at=? WHERE kind IN ('scan','semantic_control') AND status NOT IN ('completed','cancelled')",
                (MARKER, stamp),
            ).rowcount
            cards = conn.execute(
                "UPDATE zhihu_card_deliveries SET status='failed',attempts=3,last_error=? WHERE status!='delivered'",
                (MARKER,),
            ).rowcount
            conn.execute(
                "INSERT INTO zhihu_settings VALUES (?,?)",
                (
                    MARKER,
                    json.dumps(
                        {
                            "completed_at": stamp,
                            "backup": str(backup.relative_to(path.parent)),
                        }
                    ),
                ),
            )
            after = {
                name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in preserved_tables
            }
            if (
                before != after
                or delivered
                != conn.execute(
                    "SELECT count(*) FROM zhihu_link_deliveries WHERE status='delivered'"
                ).fetchone()[0]
            ):
                raise RuntimeError("User data preservation check failed")
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Upgraded database integrity check failed")
        return {
            "retired_scans": len(rows),
            "closed_jobs": jobs,
            "closed_cards": cards,
            "preserved_user_data": True,
            "integrity": "ok",
            "backup": str(backup.relative_to(path.parent)),
        }


if __name__ == "__main__":
    load_dotenv()
    print(
        json.dumps(
            upgrade(os.environ.get("SCOUT_DB_PATH", "data/scout.sqlite3")),
            ensure_ascii=False,
        )
    )
