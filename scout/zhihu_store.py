"""SQLite state for the sole owner's Zhihu topics and literal feedback.

Call initialize while holding the shared sender lock, after initializing the
legacy delivery store. Callback helpers perform only short local transactions.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import ConfigError
from .database import connect, transaction
from .storage import FeedbackError

SCHEMA_VERSION = 1
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS zhihu_settings (
        key TEXT PRIMARY KEY, value_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS zhihu_topics (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, search_terms_json TEXT NOT NULL,
        time_range TEXT NOT NULL CHECK(time_range IN ('7d','30d','all')),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS zhihu_content_snapshots (
        snapshot_id INTEGER PRIMARY KEY, content_key TEXT NOT NULL,
        content_hash TEXT NOT NULL, body TEXT NOT NULL, article_json TEXT NOT NULL,
        scan_id TEXT, created_at TEXT NOT NULL,
        UNIQUE(content_key,content_hash))""",
    """CREATE INDEX IF NOT EXISTS zhihu_snapshot_key
        ON zhihu_content_snapshots(content_key,snapshot_id DESC)""",
    """CREATE TABLE IF NOT EXISTS zhihu_candidate_discoveries (
        discovery_id TEXT PRIMARY KEY, scan_id TEXT NOT NULL,
        topic_id INTEGER NOT NULL REFERENCES zhihu_topics(id),
        content_key TEXT NOT NULL, source TEXT NOT NULL, search_term TEXT NOT NULL,
        seed_answer_id TEXT NOT NULL, question_id TEXT NOT NULL,
        list_rank INTEGER, details_json TEXT NOT NULL, collected_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS zhihu_question_expansions (
        scan_id TEXT NOT NULL, question_id TEXT NOT NULL, request_uuid TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL, ranked_answers_json TEXT NOT NULL,
        progress_json TEXT NOT NULL, error TEXT NOT NULL, collected_at TEXT NOT NULL,
        PRIMARY KEY(scan_id,question_id))""",
    """CREATE TABLE IF NOT EXISTS zhihu_candidate_results (
        scan_id TEXT NOT NULL, topic_id INTEGER NOT NULL REFERENCES zhihu_topics(id),
        content_key TEXT NOT NULL, snapshot_id INTEGER REFERENCES zhihu_content_snapshots(snapshot_id),
        article_json TEXT NOT NULL, reason TEXT NOT NULL, score REAL,
        model_version INTEGER, updated_at TEXT NOT NULL,
        PRIMARY KEY(scan_id,topic_id,content_key))""",
    """CREATE TABLE IF NOT EXISTS zhihu_feedback_revisions (
        revision_id INTEGER PRIMARY KEY, content_key TEXT NOT NULL,
        snapshot_id INTEGER NOT NULL REFERENCES zhihu_content_snapshots(snapshot_id),
        label TEXT NOT NULL CHECK(label IN ('like','dislike')),
        keywords_json TEXT NOT NULL, event_id TEXT UNIQUE, message_id TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS zhihu_feedback_content
        ON zhihu_feedback_revisions(content_key,revision_id DESC)""",
    """CREATE TABLE IF NOT EXISTS zhihu_learning_versions (
        version INTEGER PRIMARY KEY, cutoff_revision_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ready','calibrating','failed')),
        model_json TEXT NOT NULL, error TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS zhihu_jobs (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        event_id TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
        last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS zhihu_card_deliveries (
        id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, card_json TEXT NOT NULL,
        send_uuid TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','sending','delivered','failed')),
        attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '', message_id TEXT, delivered_at REAL,
        created_at TEXT NOT NULL, event_id TEXT UNIQUE,
        snapshot_id INTEGER REFERENCES zhihu_content_snapshots(snapshot_id))""",
    """CREATE TABLE IF NOT EXISTS scout_owner (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), open_id TEXT NOT NULL UNIQUE,
        bound_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))""",
)


def _now():
    return datetime.now(UTC).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def initialize(database):
    """Back up all existing state before applying the topic/learning migration."""
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path, rows=True)) as conn:
        has_settings = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='zhihu_settings'"
        ).fetchone()
        if has_settings:
            version = conn.execute(
                "SELECT value_json FROM zhihu_settings WHERE key='schema_version'"
            ).fetchone()
            if version and json.loads(version[0]) >= SCHEMA_VERSION:
                return
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone():
            backup = path.with_name(
                f"{path.stem}-before-zhihu-topics-{time.time_ns()}.sqlite3"
            )
            with closing(connect(backup)) as destination:
                conn.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchall() != [
                    ("ok",)
                ]:
                    raise ConfigError("知乎话题迁移备份完整性检查失败")
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            for statement in SCHEMA:
                conn.execute(statement)
            # Existing rows keep their original message format and stable UUID.
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='zhihu_link_deliveries'"
            ).fetchone():
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(zhihu_link_deliveries)")
                }
                for name, definition in (
                    ("message_type", "TEXT NOT NULL DEFAULT 'text'"),
                    ("card_json", "TEXT"),
                    (
                        "snapshot_id",
                        "INTEGER REFERENCES zhihu_content_snapshots(snapshot_id)",
                    ),
                ):
                    if name not in columns:
                        conn.execute(
                            f"ALTER TABLE zhihu_link_deliveries ADD COLUMN {name} {definition}"
                        )
            conn.execute(
                "INSERT OR REPLACE INTO zhihu_settings VALUES ('schema_version',?)",
                (_json(SCHEMA_VERSION),),
            )


def content_key(article):
    """Derive identity only from the explicit platform content type and ID."""
    kind, ident = article.get("content_type"), str(article.get("content_id", ""))
    if kind not in {"answer", "article"} or not re.fullmatch(r"[0-9]+", ident):
        supplied = article.get("content_key", article.get("article_key", ""))
        if not isinstance(supplied, str) or not re.fullmatch(
            r"zhihu:(answer|article):[0-9]+", supplied
        ):
            raise FeedbackError("知乎内容缺少稳定回答或文章 ID")
        return supplied
    return f"zhihu:{kind}:{ident}"


def _topic(row):
    if row is None:
        return None
    value = dict(row)
    value["search_terms"] = json.loads(value.pop("search_terms_json"))
    value["enabled"] = bool(value["enabled"])
    return value


def _terms(value):
    if isinstance(value, str):
        value = re.split(r"[,，\n]+", value)
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(term, str) for term in value
    ):
        raise FeedbackError("搜索词必须是文字列表")
    result, seen = [], set()
    for term in value:
        term = term.strip().casefold()
        if term and term not in seen:
            result.append(term)
            seen.add(term)
    if not result or len(result) > 30 or any(len(term) > 200 for term in result):
        raise FeedbackError("请填写 1–30 个搜索词，每个最多 200 字")
    return result


def save_topic(
    database,
    name,
    search_terms,
    time_range="30d",
    topic_id=None,
    enabled=True,
    event_id="",
):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise FeedbackError("话题名称需为 1–100 字")
    time_range = {7: "7d", 30: "30d", "7": "7d", "30": "30d", "unlimited": "all"}.get(
        time_range, time_range
    )
    if time_range not in {"7d", "30d", "all"}:
        raise FeedbackError("发布时间范围应为近 7 天、近 30 天或不限")
    terms, stamp = _terms(search_terms), _now()
    with transaction(database, rows=True, timeout=0.3) as conn:
        event_key = "topic_event:" + event_id
        if event_id:
            saved = conn.execute(
                "SELECT value_json FROM zhihu_settings WHERE key=?", (event_key,)
            ).fetchone()
            if saved:
                return json.loads(saved[0])
        if topic_id is None:
            cursor = conn.execute(
                """INSERT INTO zhihu_topics
                (name,search_terms_json,time_range,enabled,created_at,updated_at)
                VALUES (?,?,?,?,?,?)""",
                (
                    name.strip(),
                    _json(terms),
                    time_range,
                    int(bool(enabled)),
                    stamp,
                    stamp,
                ),
            )
            topic_id = cursor.lastrowid
        else:
            cursor = conn.execute(
                "UPDATE zhihu_topics SET name=?,search_terms_json=?,time_range=?,enabled=?,updated_at=? WHERE id=?",
                (
                    name.strip(),
                    _json(terms),
                    time_range,
                    int(bool(enabled)),
                    stamp,
                    topic_id,
                ),
            )
            if not cursor.rowcount:
                raise FeedbackError("话题不存在")
        result = _topic(
            conn.execute(
                "SELECT * FROM zhihu_topics WHERE id=?", (topic_id,)
            ).fetchone()
        )
        if event_id:
            conn.execute(
                "INSERT INTO zhihu_settings VALUES (?,?)", (event_key, _json(result))
            )
        return result


def list_topics(database, enabled_only=False):
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        return [
            _topic(row)
            for row in conn.execute(
                "SELECT * FROM zhihu_topics"
                + (" WHERE enabled=1" if enabled_only else "")
                + " ORDER BY id"
            )
        ]


def get_topic(database, topic_id):
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        return _topic(
            conn.execute(
                "SELECT * FROM zhihu_topics WHERE id=?", (topic_id,)
            ).fetchone()
        )


def set_topic_enabled(database, topic_id, enabled):
    with transaction(database, timeout=0.3) as conn:
        cursor = conn.execute(
            "UPDATE zhihu_topics SET enabled=?,updated_at=? WHERE id=?",
            (int(bool(enabled)), _now(), topic_id),
        )
        if not cursor.rowcount:
            raise FeedbackError("话题不存在")
    return get_topic(database, topic_id)


def get_setting(database, key, default=None):
    with closing(connect(database, read_only=True, timeout=0.3)) as conn:
        row = conn.execute(
            "SELECT value_json FROM zhihu_settings WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default


def set_setting(database, key, value):
    with transaction(database, timeout=0.3) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO zhihu_settings VALUES (?,?)", (key, _json(value))
        )


def get_schedule(database):
    return get_setting(
        database,
        "daily_schedule",
        {"enabled": False, "time_of_day": None, "timezone": "Asia/Shanghai"},
    )


def set_schedule(database, enabled, time_of_day=None, timezone="Asia/Shanghai"):
    if enabled and (
        not isinstance(time_of_day, str)
        or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", time_of_day)
    ):
        raise FeedbackError("启用每日扫描时必须指定执行时刻 HH:MM")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError, TypeError, ValueError:
        raise FeedbackError("扫描时区无效") from None
    value = {
        "enabled": bool(enabled),
        "time_of_day": time_of_day if enabled else None,
        "timezone": timezone,
    }
    if enabled:
        now = datetime.now(ZoneInfo(timezone))
        first_day = now.date()
        if now.strftime("%H:%M") > time_of_day:
            first_day += timedelta(days=1)
        value["first_run_date"] = first_day.isoformat()
    set_setting(database, "daily_schedule", value)
    return value


def authorize_owner(database, open_id):
    """Call only after validating the source card and the proposed operation."""
    if not isinstance(open_id, str) or not open_id.strip():
        raise FeedbackError("缺少飞书操作人身份")
    with transaction(database, timeout=0.3) as conn:
        owner = conn.execute(
            "SELECT open_id FROM scout_owner WHERE singleton=1"
        ).fetchone()
        if owner is None:
            conn.execute(
                "INSERT INTO scout_owner(singleton,open_id) VALUES (1,?)", (open_id,)
            )
        elif owner[0] != open_id:
            raise FeedbackError("仅 Scout 所有者可以操作知乎话题及反馈")


def authorize_card(database, message_id, chat_id, snapshot_id=None):
    if not message_id or not chat_id:
        raise FeedbackError("缺少卡片消息或群身份")
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        for table in ("zhihu_card_deliveries", "zhihu_link_deliveries"):
            row = conn.execute(
                f"SELECT snapshot_id FROM {table} WHERE message_id=? AND chat_id=? AND status='delivered'",
                (message_id, chat_id),
            ).fetchone()
            if row and (snapshot_id is None or row["snapshot_id"] == snapshot_id):
                return
    raise FeedbackError("卡片与已送达的 Scout 消息不一致")


def save_content(database, article, scan_id=None):
    key = content_key(article)
    body = article.get("body", "")
    if not isinstance(body, str) or not body.strip() or article.get("status") != "body":
        raise FeedbackError("仅完整正文可以保存为知乎反馈快照")
    value = {**article, "content_key": key, "article_key": key}
    # Preserve the exact displayed snapshot, including publication and body.
    digest = hashlib.sha256(_json(value).encode()).hexdigest()
    with transaction(database, rows=True, timeout=0.3) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO zhihu_content_snapshots
            (content_key,content_hash,body,article_json,scan_id,created_at) VALUES (?,?,?,?,?,?)""",
            (key, digest, body, _json(value), scan_id, _now()),
        )
        row = conn.execute(
            "SELECT * FROM zhihu_content_snapshots WHERE content_key=? AND content_hash=?",
            (key, digest),
        ).fetchone()
        return _content(row)


def _content(row):
    if row is None:
        return None
    return {
        **json.loads(row["article_json"]),
        "snapshot_id": row["snapshot_id"],
        "content_key": row["content_key"],
        "article_key": row["content_key"],
    }


def get_content(database, snapshot_id=None, content_key=None):
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        if snapshot_id is not None:
            row = conn.execute(
                "SELECT * FROM zhihu_content_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM zhihu_content_snapshots WHERE content_key=? ORDER BY snapshot_id DESC LIMIT 1",
                (content_key,),
            ).fetchone()
        return _content(row)


def normalize_keywords(keywords, body):
    if isinstance(keywords, str):
        keywords = re.split(r"[,，]", keywords)
    if not isinstance(keywords, (list, tuple)) or any(
        not isinstance(word, str) for word in keywords
    ):
        raise FeedbackError("降权关键词需用中英文逗号分隔")
    result = list(
        dict.fromkeys(word.strip().casefold() for word in keywords if word.strip())
    )
    if not result:
        raise FeedbackError("不喜欢必须填写至少一个降权关键词")
    if len(result) > 100 or any(len(word) > 200 for word in result):
        raise FeedbackError("最多填写 100 个降权关键词，每个最多 200 字")
    folded = body.casefold()
    missing = [word for word in result if word not in folded]
    if missing:
        raise FeedbackError("以下关键词未在该篇正文中出现：" + "、".join(missing))
    return result


def _feedback(row):
    if row is None:
        return None
    value = dict(row)
    value["keywords"] = json.loads(value.pop("keywords_json"))
    return value


def latest_feedback(database, content_key):
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        return _feedback(
            conn.execute(
                "SELECT * FROM zhihu_feedback_revisions WHERE content_key=? ORDER BY revision_id DESC LIMIT 1",
                (content_key,),
            ).fetchone()
        )


def save_feedback(
    database, snapshot_id, label, keywords="", event_id="", message_id=""
):
    if label not in {"like", "dislike"}:
        raise FeedbackError("反馈只能为喜欢或不喜欢")
    with transaction(database, rows=True, timeout=0.3) as conn:
        article = conn.execute(
            "SELECT * FROM zhihu_content_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if article is None:
            raise FeedbackError("找不到该篇完整正文快照")
        words = (
            normalize_keywords(keywords, article["body"]) if label == "dislike" else []
        )
        existing = (
            conn.execute(
                "SELECT * FROM zhihu_feedback_revisions WHERE event_id=?", (event_id,)
            ).fetchone()
            if event_id
            else None
        )
        if event_id and existing is None:
            event = conn.execute(
                "SELECT value_json FROM zhihu_settings WHERE key=?",
                ("feedback_event:" + event_id,),
            ).fetchone()
            if event:
                existing = conn.execute(
                    "SELECT * FROM zhihu_feedback_revisions WHERE revision_id=?",
                    (json.loads(event[0]),),
                ).fetchone()
        if existing:
            if (
                existing["content_key"] != article["content_key"]
                or existing["snapshot_id"] != snapshot_id
                or existing["label"] != label
                or json.loads(existing["keywords_json"]) != words
            ):
                raise FeedbackError("同一回调标识对应了不同反馈")
            return {**_feedback(existing), "created": False}
        previous = conn.execute(
            "SELECT * FROM zhihu_feedback_revisions WHERE content_key=? ORDER BY revision_id DESC LIMIT 1",
            (article["content_key"],),
        ).fetchone()
        if (
            previous
            and previous["label"] == label
            and previous["snapshot_id"] == snapshot_id
            and json.loads(previous["keywords_json"]) == words
        ):
            if event_id:
                conn.execute(
                    "INSERT INTO zhihu_settings VALUES (?,?)",
                    ("feedback_event:" + event_id, _json(previous["revision_id"])),
                )
            return {**_feedback(previous), "created": False}
        cursor = conn.execute(
            """INSERT INTO zhihu_feedback_revisions
            (content_key,snapshot_id,label,keywords_json,event_id,message_id,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (
                article["content_key"],
                snapshot_id,
                label,
                _json(words),
                event_id or None,
                message_id,
                _now(),
            ),
        )
        revision = cursor.lastrowid
        conn.execute(
            "INSERT OR REPLACE INTO zhihu_settings VALUES ('training_requested_revision',?)",
            (_json(revision),),
        )
        return {
            **_feedback(
                conn.execute(
                    "SELECT * FROM zhihu_feedback_revisions WHERE revision_id=?",
                    (revision,),
                ).fetchone()
            ),
            "created": True,
        }


def save_discovery(
    database,
    scan_id,
    topic_id,
    content_key,
    source,
    search_term="",
    seed_answer_id="",
    question_id="",
    rank=None,
    details=None,
):
    basis = [
        scan_id,
        topic_id,
        content_key,
        source,
        search_term,
        seed_answer_id,
        question_id,
        rank,
    ]
    ident = hashlib.sha256(_json(basis).encode()).hexdigest()
    with transaction(database, timeout=0.3) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO zhihu_candidate_discoveries
            (discovery_id,scan_id,topic_id,content_key,source,search_term,seed_answer_id,question_id,list_rank,details_json,collected_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ident, *basis, _json(details or {}), _now()),
        )


def save_expansion(
    database,
    scan_id,
    question_id,
    request_uuid,
    status="pending",
    ranked_answers=None,
    progress=None,
    error="",
):
    with transaction(database, timeout=0.3) as conn:
        conn.execute(
            """INSERT INTO zhihu_question_expansions VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(scan_id,question_id) DO UPDATE SET
            status=excluded.status,ranked_answers_json=excluded.ranked_answers_json,
            progress_json=excluded.progress_json,error=excluded.error,collected_at=excluded.collected_at""",
            (
                scan_id,
                question_id,
                request_uuid,
                status,
                _json(ranked_answers or []),
                _json(progress or {}),
                error,
                _now(),
            ),
        )


def list_expansions(database, scan_id):
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        result = []
        for row in conn.execute(
            "SELECT * FROM zhihu_question_expansions WHERE scan_id=? ORDER BY rowid",
            (scan_id,),
        ):
            value = dict(row)
            value["ranked_answers"] = json.loads(value.pop("ranked_answers_json"))
            value["progress"] = json.loads(value.pop("progress_json"))
            result.append(value)
        return result


def save_candidate_result(
    database,
    scan_id,
    topic_id,
    article,
    snapshot_id=None,
    reason="",
    score=None,
    model_version=None,
):
    with transaction(database, timeout=0.3) as conn:
        conn.execute(
            """INSERT INTO zhihu_candidate_results VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(scan_id,topic_id,content_key) DO UPDATE SET
            snapshot_id=excluded.snapshot_id,article_json=excluded.article_json,
            reason=excluded.reason,score=excluded.score,model_version=excluded.model_version,updated_at=excluded.updated_at""",
            (
                scan_id,
                topic_id,
                content_key(article),
                snapshot_id,
                _json(article),
                reason,
                score,
                model_version,
                _now(),
            ),
        )


def list_filtered(database, topic_id=None, limit=20, offset=0):
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        clauses, values = (
            [
                "reason IN ('keyword_filtered','preference','filtered','keyword','date_unknown','date_expired','date_future','body_incomplete','body_failed')"
            ],
            [],
        )
        if topic_id is not None:
            clauses.append("topic_id=?")
            values.append(topic_id)
        rows = conn.execute(
            "SELECT * FROM zhihu_candidate_results WHERE "
            + " AND ".join(clauses)
            + " ORDER BY CASE WHEN reason='keyword_filtered' THEN 0 ELSE 1 END,updated_at DESC LIMIT ? OFFSET ?",
            (*values, min(100, max(1, int(limit))), max(0, int(offset))),
        )
        result = []
        for row in rows:
            value = dict(row)
            article = json.loads(value.pop("article_json"))
            result.append({**article, **value, "article_key": value["content_key"]})
        return result


def _job(row):
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def enqueue_job(database, kind, payload, event_id=""):
    stamp = _now()
    with transaction(database, rows=True, timeout=0.3) as conn:
        if event_id:
            row = conn.execute(
                "SELECT * FROM zhihu_jobs WHERE event_id=?", (event_id,)
            ).fetchone()
            if row:
                if row["kind"] != kind or json.loads(row["payload_json"]) != payload:
                    raise FeedbackError("同一回调标识对应了不同任务")
                return _job(row)
        cursor = conn.execute(
            "INSERT INTO zhihu_jobs(kind,payload_json,event_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            (kind, _json(payload), event_id or None, stamp, stamp),
        )
        return _job(
            conn.execute(
                "SELECT * FROM zhihu_jobs WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        )


def pending_jobs(database, kind=None):
    with closing(connect(database, read_only=True, rows=True, timeout=0.3)) as conn:
        query, values = (
            "SELECT * FROM zhihu_jobs WHERE status IN ('pending','running')",
            (),
        )
        if kind:
            query += " AND kind=?"
            values = (kind,)
        return [_job(row) for row in conn.execute(query + " ORDER BY id", values)]


def start_job(database, job_id):
    with transaction(database, timeout=0.3) as conn:
        conn.execute(
            "UPDATE zhihu_jobs SET status='running',updated_at=? WHERE id=? AND status='pending'",
            (_now(), job_id),
        )


def finish_job(database, job_id, error=""):
    with transaction(database, timeout=0.3) as conn:
        conn.execute(
            "UPDATE zhihu_jobs SET status=?,last_error=?,updated_at=? WHERE id=?",
            ("failed" if error else "completed", error, _now(), job_id),
        )


def enqueue_card(database, card, chat_id, event_id="", snapshot_id=None):
    """Reserve first review deliveries under the automatic stable identity too.

    An explicit repeat review may use the separate card queue, but the first
    review must block concurrent/future automatic selection before HTTP starts.
    """
    if not isinstance(card, dict) or not isinstance(chat_id, str) or not chat_id:
        raise FeedbackError("卡片或目标群无效")
    with transaction(database, rows=True, timeout=0.3) as conn:
        if event_id:
            row = conn.execute(
                "SELECT * FROM zhihu_card_deliveries WHERE event_id=?", (event_id,)
            ).fetchone()
            if row:
                return dict(row)
            review_event = conn.execute(
                "SELECT value_json FROM zhihu_settings WHERE key=?",
                ("review_event:" + event_id,),
            ).fetchone()
            if review_event:
                return dict(
                    conn.execute(
                        "SELECT * FROM zhihu_link_deliveries WHERE position=?",
                        (json.loads(review_event[0]),),
                    ).fetchone()
                )
        if snapshot_id is not None:
            article = conn.execute(
                "SELECT content_key,article_json,scan_id FROM zhihu_content_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if article is None:
                raise FeedbackError("找不到该篇完整正文快照")
            reserved = conn.execute(
                "SELECT * FROM zhihu_link_deliveries WHERE article_key=?",
                (article["content_key"],),
            ).fetchone()
            if reserved is not None and reserved["status"] != "delivered":
                # Repeated review clicks cannot race a pending automatic or
                # review delivery. An explicit retry revives the original UUID.
                if reserved["status"] == "failed":
                    conn.execute(
                        """UPDATE zhihu_link_deliveries SET status='pending',
                        attempts=0,retry_at=0,last_error='' WHERE position=?""",
                        (reserved["position"],),
                    )
                if event_id:
                    conn.execute(
                        "INSERT INTO zhihu_settings VALUES (?,?)",
                        ("review_event:" + event_id, _json(reserved["position"])),
                    )
                return dict(
                    conn.execute(
                        "SELECT * FROM zhihu_link_deliveries WHERE position=?",
                        (reserved["position"],),
                    ).fetchone()
                )
            if reserved is None:
                if not article["scan_id"]:
                    raise FeedbackError("正文快照缺少原扫描记录，无法保留送达身份")
                details = json.loads(article["article_json"])
                cursor = conn.execute(
                    """INSERT INTO zhihu_link_deliveries
                    (article_key,scan_id,title,url,chat_id,send_uuid,message_type,card_json,snapshot_id)
                    VALUES (?,?,?,?,?,?,'interactive_review',?,?)""",
                    (
                        article["content_key"],
                        article["scan_id"],
                        details["title"],
                        details["url"],
                        chat_id,
                        str(
                            uuid.uuid5(
                                uuid.UUID(article["scan_id"]),
                                "review:" + article["content_key"],
                            )
                        ),
                        _json(card),
                        snapshot_id,
                    ),
                )
                if event_id:
                    conn.execute(
                        "INSERT INTO zhihu_settings VALUES (?,?)",
                        ("review_event:" + event_id, _json(cursor.lastrowid)),
                    )
                return dict(
                    conn.execute(
                        "SELECT * FROM zhihu_link_deliveries WHERE position=?",
                        (cursor.lastrowid,),
                    ).fetchone()
                )
        cursor = conn.execute(
            "INSERT INTO zhihu_card_deliveries(chat_id,card_json,send_uuid,created_at,event_id,snapshot_id) VALUES (?,?,?,?,?,?)",
            (
                chat_id,
                _json(card),
                str(uuid.uuid4()),
                _now(),
                event_id or None,
                snapshot_id,
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM zhihu_card_deliveries WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        )


def delivered_keys(database):
    """All reserved identities plus historical successful validation deliveries."""
    with closing(connect(database, read_only=True, timeout=0.3)) as conn:
        result = {
            row[0]
            for row in conn.execute("SELECT article_key FROM zhihu_link_deliveries")
        }
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='rule_test_deliveries'"
        ).fetchone():
            result.update(
                row[0]
                for row in conn.execute(
                    "SELECT article_key FROM rule_test_deliveries WHERE status='delivered'"
                )
            )
        return result
