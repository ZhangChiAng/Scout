"""Durable, ordered Zhihu title/link delivery. Network calls hold no transaction."""

import time
import uuid
from contextlib import closing
from pathlib import Path

from .config import ConfigError, resolve_feishu_delivery
from .database import connect, transaction
from .locking import RunLockedError, sender_lock
from .notifier import FeishuNotifier, NotificationError
from .rules import Rules
from .zhihu_verify import evaluate_article, validate_evidence

SCHEMA = """CREATE TABLE IF NOT EXISTS zhihu_link_deliveries (
    position INTEGER PRIMARY KEY,
    article_key TEXT NOT NULL UNIQUE,
    scan_id TEXT NOT NULL REFERENCES zhihu_scans(id),
    title TEXT NOT NULL, url TEXT NOT NULL, chat_id TEXT NOT NULL,
    send_uuid TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','delivered','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
    retry_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '', message_id TEXT,
    delivered_at REAL
)"""


def initialize(database):
    """Caller owns sender lock. Back up existing state before enabling the new table."""
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='zhihu_link_deliveries'"
        ).fetchone():
            return
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone():
            backup = path.with_name(
                f"{path.stem}-before-links-{time.time_ns()}.sqlite3"
            )
            with closing(connect(backup)) as destination:
                conn.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchall() != [
                    ("ok",)
                ]:
                    raise ConfigError("迁移备份完整性检查失败")
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS zhihu_scans (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                status TEXT NOT NULL, state_json TEXT NOT NULL
            )""")
            conn.execute(SCHEMA)


def register(database, state):
    """Reconcile saved bodies after crashes, preserving discovery order and quota."""
    if not state.get("auto_notify"):
        return 0
    rules = Rules.load(state["config"]["rules"])
    with transaction(database) as conn:
        count = conn.execute(
            "SELECT count(*) FROM zhihu_link_deliveries WHERE scan_id=?", (state["id"],)
        ).fetchone()[0]
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='rule_test_deliveries'"
        ).fetchone()
        records = sorted(
            state["records"],
            key=lambda a: (
                a["first_discovery"]["query_index"],
                a["first_discovery"]["result_index"],
            ),
        )
        for article in records:
            if count >= state["config"]["scan"]["max_results"]:
                break
            if not evaluate_article(article, rules)["eligible"]:
                continue
            key = article["article_key"]
            if (
                legacy
                and conn.execute(
                    "SELECT 1 FROM rule_test_deliveries WHERE article_key=? AND status='delivered'",
                    (key,),
                ).fetchone()
            ):
                continue
            if conn.execute(
                "SELECT 1 FROM zhihu_link_deliveries WHERE article_key=?", (key,)
            ).fetchone():
                continue
            validate_evidence(Path(state["directory"]), article)
            conn.execute(
                """INSERT INTO zhihu_link_deliveries
                (article_key,scan_id,title,url,chat_id,send_uuid) VALUES (?,?,?,?,?,?)""",
                (
                    key,
                    state["id"],
                    article["title"],
                    article["url"],
                    state["chat_id"],
                    str(uuid.uuid4()),
                ),
            )
            count += 1
    return count


def delivery_summary(database, scan_id):
    with closing(connect(database, read_only=True)) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='zhihu_link_deliveries'"
        ).fetchone():
            return {}
        return dict(
            conn.execute(
                "SELECT status,count(*) FROM zhihu_link_deliveries WHERE scan_id=? GROUP BY status",
                (scan_id,),
            )
        )


def reset_failed(database, scan_id):
    with sender_lock(database), transaction(database) as conn:
        conn.execute(
            "UPDATE zhihu_link_deliveries SET status='pending',attempts=0,retry_at=0,last_error='' WHERE scan_id=? AND (status='failed' OR (status='sending' AND attempts>=3))",
            (scan_id,),
        )


def recover(database):
    try:
        with sender_lock(database):
            _recover(database)
    except RunLockedError:
        return


def _recover(database):
    with closing(connect(database, read_only=True, rows=True)) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='zhihu_link_deliveries'"
        ).fetchone():
            return
        # Only the first undelivered row in each scan may advance. A failed row
        # pauses its scan; other scans remain independent.
        rows = conn.execute("""SELECT * FROM zhihu_link_deliveries d
            WHERE status IN ('pending','sending') AND NOT EXISTS (
                SELECT 1 FROM zhihu_link_deliveries older WHERE older.scan_id=d.scan_id
                AND older.position<d.position AND older.status!='delivered')
            ORDER BY position""").fetchall()
    for row in rows:
        if row["retry_at"] > time.time():
            continue
        if row["attempts"] >= 3:
            with transaction(database) as conn:
                conn.execute(
                    "UPDATE zhihu_link_deliveries SET status='failed' WHERE position=?",
                    (row["position"],),
                )
            continue
        # Reserve the attempt before HTTP. A crash has an unknown outcome and
        # consumes this attempt; the same UUID is retained for recovery.
        attempt = row["attempts"] + 1
        with transaction(database) as conn:
            conn.execute(
                "UPDATE zhihu_link_deliveries SET status='sending',attempts=?,retry_at=? WHERE position=?",
                (attempt, time.time() + (5 if attempt == 1 else 15), row["position"]),
            )
        try:
            notifier = FeishuNotifier(resolve_feishu_delivery(), 15)
            sent = notifier.send_link(
                row["title"],
                row["url"],
                chat_id=row["chat_id"],
                send_uuid=row["send_uuid"],
            )
            if sent.chat_id != row["chat_id"]:
                raise NotificationError("返回群与固定目标群不符")
        except (NotificationError, ConfigError) as exc:
            with transaction(database) as conn:
                conn.execute(
                    "UPDATE zhihu_link_deliveries SET status=?,last_error=?,retry_at=? WHERE position=?",
                    (
                        "failed" if attempt == 3 else "pending",
                        type(exc).__name__,
                        time.time() + (5 if attempt == 1 else 15),
                        row["position"],
                    ),
                )
        else:
            with transaction(database) as conn:
                conn.execute(
                    "UPDATE zhihu_link_deliveries SET status='delivered',message_id=?,delivered_at=?,last_error='' WHERE position=?",
                    (sent.message_id, time.time(), row["position"]),
                )
