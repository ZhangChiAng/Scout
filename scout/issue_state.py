"""Durable daily lists and owner-requested article delivery, using short transactions."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .database import transaction
from .model import DigestArticle, NewsItem, PersonalizedEvaluation

if TYPE_CHECKING:
    from .storage import SQLiteStorage


ISSUE_SCHEMAS = (
    """CREATE TABLE IF NOT EXISTS issue_snapshots (
        source TEXT NOT NULL,
        issue_date TEXT NOT NULL,
        digest_key TEXT NOT NULL,
        item_json TEXT NOT NULL,
        articles_json TEXT NOT NULL,
        PRIMARY KEY (source, issue_date)
    )""",
    """CREATE TABLE IF NOT EXISTS filtered_lists (
        list_id INTEGER PRIMARY KEY,
        digest_key TEXT NOT NULL,
        issue_date TEXT NOT NULL,
        page INTEGER NOT NULL,
        pages INTEGER NOT NULL,
        send_uuid TEXT NOT NULL UNIQUE,
        message_id TEXT UNIQUE,
        chat_id TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0,
        synced_revision INTEGER NOT NULL DEFAULT -1,
        sync_attempts INTEGER NOT NULL DEFAULT 0,
        sync_after REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        UNIQUE (digest_key, page)
    )""",
    """CREATE TABLE IF NOT EXISTS filtered_list_members (
        member_id INTEGER PRIMARY KEY,
        list_id INTEGER NOT NULL REFERENCES filtered_lists(list_id),
        snapshot_id INTEGER NOT NULL REFERENCES article_snapshots(snapshot_id),
        article_key TEXT NOT NULL UNIQUE,
        position INTEGER NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict = '不推荐'),
        reason TEXT NOT NULL,
        profile_version INTEGER NOT NULL,
        UNIQUE (list_id, position)
    )""",
    """CREATE TABLE IF NOT EXISTS reveal_submissions (
        submission_id INTEGER PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        list_id INTEGER NOT NULL REFERENCES filtered_lists(list_id),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS reveal_requests (
        member_id INTEGER PRIMARY KEY REFERENCES filtered_list_members(member_id),
        article_key TEXT NOT NULL UNIQUE,
        submission_id INTEGER NOT NULL REFERENCES reveal_submissions(submission_id),
        send_uuid TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'delivered', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
        retry_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        delivery_id INTEGER REFERENCES card_deliveries(delivery_id)
    )""",
)


@dataclass(frozen=True, slots=True)
class ListMember:
    member_id: int
    snapshot_id: int
    article: DigestArticle
    evaluation: PersonalizedEvaluation
    profile_version: int
    status: str = "available"


@dataclass(frozen=True, slots=True)
class FilteredList:
    list_id: int
    digest_key: str
    issue_date: str
    page: int
    pages: int
    send_uuid: str
    message_id: str | None
    chat_id: str
    revision: int
    members: tuple[ListMember, ...]


@dataclass(frozen=True, slots=True)
class RevealRequest:
    member: ListMember
    list_id: int
    chat_id: str
    send_uuid: str
    attempts: int


def _article(payload: dict) -> DigestArticle:
    return DigestArticle(
        **{
            **payload,
            "related_links": tuple(tuple(v) for v in payload["related_links"]),
        }
    )


def _news_item(payload: dict) -> NewsItem:
    # One real 2026-09-26 snapshot contains these abandoned RSS extensions.
    # Reject any other unknown field so unexpected schema changes stay visible.
    original = payload.copy()
    for key in ("first_seen_at", "updated_at", "summary_html"):
        original.pop(key, None)
    return NewsItem(**original)


class IssueState:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def save_issue(
        self, item: NewsItem, issue_date: str, articles: Sequence[DigestArticle]
    ) -> None:
        # One full manifest per date; published lists keep their own immutable
        # snapshot IDs even if the publisher later edits the RSS content.
        with transaction(self.storage.path) as conn:
            conn.execute(
                """INSERT INTO issue_snapshots VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, issue_date) DO UPDATE SET
                    digest_key=excluded.digest_key, item_json=excluded.item_json,
                    articles_json=excluded.articles_json""",
                (
                    item.source,
                    issue_date,
                    item.dedupe_key,
                    json.dumps(asdict(item), ensure_ascii=False),
                    json.dumps([asdict(a) for a in articles], ensure_ascii=False),
                ),
            )

    def load_issue(
        self, source: str, issue_date: str
    ) -> tuple[NewsItem, tuple[DigestArticle, ...]] | None:
        if not self.storage.path.exists():
            return None
        with closing(self.storage._connect_read_only()) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            if "issue_snapshots" in tables:
                row = conn.execute(
                    "SELECT item_json, articles_json FROM issue_snapshots "
                    "WHERE source=? AND issue_date=?",
                    (source, issue_date),
                ).fetchone()
                if row:
                    return (
                        _news_item(json.loads(row[0])),
                        tuple(_article(a) for a in json.loads(row[1])),
                    )
            # Before manifests existed, save_article_snapshots already saved
            # the entire parsed issue atomically. Only accept one unambiguous
            # Juya issue with contiguous positions and complete body text.
            if "article_snapshots" not in tables:
                return None
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT s.* FROM article_snapshots s
                WHERE EXISTS (SELECT 1 FROM source_state WHERE source=?)
                AND s.digest_key = ?
                AND s.snapshot_id = (
                    SELECT max(s2.snapshot_id) FROM article_snapshots s2
                    WHERE s2.digest_key=s.digest_key AND s2.position=s.position
                ) ORDER BY s.position""",
                (source, f"https://daily.juya.uk/issues/{issue_date}/"),
            ).fetchall()
            if not rows or len({r["digest_key"] for r in rows}) != 1:
                return None
            if [r["position"] for r in rows] != list(range(1, len(rows) + 1)):
                return None
            if any(not r["summary"] or not r["detail"] for r in rows):
                return None
            articles = tuple(self._snapshot_article(r) for r in rows)
            key = rows[0]["digest_key"]
            item = NewsItem(
                source,
                key,
                f"橘鸦 AI 早报 {issue_date}",
                "",
                key,
                issue_date,
                "",
                "",
                key,
                key,
            )
            return item, articles

    def saved_issues(
        self, source: str
    ) -> tuple[tuple[str, NewsItem, tuple[DigestArticle, ...]], ...]:
        """Read only committed full manifests; partial article rows are insufficient."""
        if not self.storage.path.exists():
            return ()
        with closing(self.storage._connect_read_only()) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='issue_snapshots'"
            ).fetchone():
                return ()
            rows = conn.execute(
                "SELECT issue_date, item_json, articles_json FROM issue_snapshots "
                "WHERE source=? ORDER BY issue_date",
                (source,),
            ).fetchall()
        return tuple(
            (
                day,
                _news_item(json.loads(item)),
                tuple(_article(a) for a in json.loads(articles)),
            )
            for day, item, articles in rows
            if json.loads(articles)
        )

    def first_saved_at(self, digest_key: str) -> str:
        with closing(self.storage._connect_read_only()) as conn:
            return (
                conn.execute(
                    "SELECT min(captured_at) FROM article_snapshots WHERE digest_key=?",
                    (digest_key,),
                ).fetchone()[0]
                or "unknown"
            )

    def presented(self, article_key: str) -> bool:
        if self.storage.is_article_delivered(article_key, read_only=True):
            return True
        if not self.storage.path.exists():
            return False
        with closing(self.storage._connect_read_only()) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='filtered_lists'"
            ).fetchone():
                return False
            return (
                conn.execute(
                    """SELECT 1 FROM filtered_list_members m
                JOIN filtered_lists l USING(list_id)
                WHERE m.article_key=? AND l.message_id IS NOT NULL""",
                    (article_key,),
                ).fetchone()
                is not None
            )

    def lists(self, digest_key: str) -> tuple[FilteredList, ...]:
        if not self.storage.path.exists():
            return ()
        with closing(self.storage._connect_read_only()) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='filtered_lists'"
            ).fetchone():
                return ()
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT list_id FROM filtered_lists WHERE digest_key=? ORDER BY page",
                    (digest_key,),
                )
            ]
            return tuple(self._read_list(conn, value) for value in ids)

    def create_lists(
        self,
        *,
        digest_key: str,
        issue_date: str,
        chat_id: str,
        pages: Sequence[Sequence[ListMember]],
    ) -> tuple[FilteredList, ...]:
        with transaction(self.storage.path) as conn:
            if not conn.execute(
                "SELECT 1 FROM filtered_lists WHERE digest_key=?", (digest_key,)
            ).fetchone():
                for page, members in enumerate(pages, 1):
                    cursor = conn.execute(
                        """INSERT INTO filtered_lists
                        (digest_key, issue_date, page, pages, send_uuid, chat_id)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            digest_key,
                            issue_date,
                            page,
                            len(pages),
                            str(uuid.uuid4()),
                            chat_id,
                        ),
                    )
                    conn.executemany(
                        """INSERT INTO filtered_list_members
                        (list_id, snapshot_id, article_key, position, verdict,
                         reason, profile_version) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        [
                            (
                                cursor.lastrowid,
                                m.snapshot_id,
                                m.article.article_key,
                                m.article.position,
                                m.evaluation.verdict,
                                m.evaluation.reason,
                                m.profile_version,
                            )
                            for m in members
                        ],
                    )
        return self.lists(digest_key)

    def mark_list_sent(self, list_id: int, message_id: str, chat_id: str) -> None:
        with transaction(self.storage.path) as conn:
            conn.execute(
                """UPDATE filtered_lists SET message_id=?, chat_id=?,
                synced_revision=revision WHERE list_id=? AND message_id IS NULL""",
                (message_id, chat_id, list_id),
            )

    def enqueue(
        self,
        *,
        list_id: int,
        message_id: str,
        chat_id: str,
        open_id: str,
        event_id: str,
        form: object,
    ) -> int:
        from .storage import FeedbackError

        if not event_id or not isinstance(form, dict):
            raise FeedbackError("正文请求缺少事件或表单数据")
        # Callback must return inside Feishu's 3-second deadline even if a
        # writer is busy; a timeout is safe to retry without losing requests.
        with transaction(self.storage.path, timeout=0.5) as conn:
            owner = conn.execute(
                "SELECT open_id FROM scout_owner WHERE singleton=1"
            ).fetchone()
            if not open_id or owner is None or owner[0] != open_id:
                raise FeedbackError("仅已绑定的 Scout owner 可以请求正文")
            listing = self._read_list(conn, list_id)
            if (
                not message_id
                or listing.message_id != message_id
                or listing.chat_id != chat_id
            ):
                raise FeedbackError("列表与群消息记录不匹配")
            allowed = {f"member_{m.member_id}": m for m in listing.members}
            selected = []
            for name, checked in form.items():
                if name not in allowed or not isinstance(checked, bool):
                    raise FeedbackError("所选条目不属于此列表或勾选值无效")
                if checked:
                    selected.append(allowed[name])
            if not selected:
                raise FeedbackError("请先勾选需要推送正文的新闻")
            prior = conn.execute(
                "SELECT 1 FROM reveal_submissions WHERE event_id=?", (event_id,)
            ).fetchone()
            if prior:
                return 0
            cursor = conn.execute(
                "INSERT INTO reveal_submissions (event_id, list_id) VALUES (?, ?)",
                (event_id, list_id),
            )
            submission_id = cursor.lastrowid
            count = 0
            for m in sorted(selected, key=lambda m: m.article.position):
                if m.status not in {"available", "failed"}:
                    continue
                changed = conn.execute(
                    """INSERT INTO reveal_requests
                    (member_id, article_key, submission_id, send_uuid, status)
                    VALUES (?, ?, ?, ?, 'pending')
                    ON CONFLICT(member_id) DO UPDATE SET
                        submission_id=excluded.submission_id, status='pending',
                        attempts=0, retry_at=0, last_error=''
                    WHERE reveal_requests.status='failed'""",
                    (
                        m.member_id,
                        m.article.article_key,
                        submission_id,
                        str(uuid.uuid4()),
                    ),
                )
                count += changed.rowcount
            # The worker alone patches cards: a synchronous callback card
            # could arrive after its patch and restore stale enabled checkers.
            self._dirty(conn, list_id)
            return count

    def recover(self) -> None:
        with transaction(self.storage.path) as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    """SELECT DISTINCT m.list_id FROM reveal_requests r
                JOIN filtered_list_members m USING(member_id) WHERE r.status='sending'"""
                )
            ]
            conn.execute(
                """UPDATE reveal_requests SET status=CASE
                    WHEN attempts >= 3 THEN 'failed' ELSE 'pending' END
                WHERE status='sending'"""
            )
            for list_id in ids:
                self._dirty(conn, list_id)
            # A restart retries exhausted card patches, not failed body requests.
            conn.execute("UPDATE filtered_lists SET sync_attempts=0, sync_after=0")

    def claim(self) -> RevealRequest | None:
        with transaction(self.storage.path) as conn:
            row = conn.execute(
                """SELECT r.member_id, m.list_id, r.send_uuid, r.attempts, r.retry_at
                FROM reveal_requests r JOIN filtered_list_members m USING(member_id)
                WHERE r.status='pending'
                ORDER BY r.submission_id, m.position LIMIT 1"""
            ).fetchone()
            # Keep each selected batch in source order, including its retries.
            if row is None or row[4] > time.time():
                return None
            conn.execute(
                "UPDATE reveal_requests SET status='sending', attempts=attempts+1 WHERE member_id=?",
                (row[0],),
            )
            listing = self._read_list(conn, row[1])
            member = next(m for m in listing.members if m.member_id == row[0])
            return RevealRequest(
                member, listing.list_id, listing.chat_id, row[2], row[3] + 1
            )

    def finish(self, request: RevealRequest, *, message_id: str, chat_id: str) -> None:
        m = request.member
        with transaction(self.storage.path) as conn:
            delivery_id = self.storage._record_card_delivery(
                conn,
                snapshot_id=m.snapshot_id,
                article_key=m.article.article_key,
                purpose="personalized",
                message_id=message_id,
                chat_id=chat_id,
                evaluation=m.evaluation,
            )
            conn.execute(
                "UPDATE reveal_requests SET status='delivered', delivery_id=?, last_error='' WHERE member_id=?",
                (delivery_id, m.member_id),
            )
            self._dirty(conn, request.list_id)

    def reconcile_delivery(self, request: RevealRequest) -> bool:
        with transaction(self.storage.path) as conn:
            row = conn.execute(
                "SELECT delivery_id FROM card_deliveries WHERE article_key=? LIMIT 1",
                (request.member.article.article_key,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE reveal_requests SET status='delivered', delivery_id=? WHERE member_id=?",
                (row[0], request.member.member_id),
            )
            self._dirty(conn, request.list_id)
            return True

    def fail(self, request: RevealRequest, error: str) -> None:
        with transaction(self.storage.path) as conn:
            conn.execute(
                """UPDATE reveal_requests SET status=?, retry_at=?, last_error=?
                WHERE member_id=? AND status='sending'""",
                (
                    "failed" if request.attempts >= 3 else "pending",
                    time.time() + 2**request.attempts,
                    error[:500],
                    request.member.member_id,
                ),
            )
            self._dirty(conn, request.list_id)

    def dirty_lists(self) -> tuple[FilteredList, ...]:
        with closing(self.storage._connect_read_only()) as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    """SELECT list_id FROM filtered_lists WHERE message_id IS NOT NULL
                AND synced_revision < revision AND sync_attempts < 3 AND sync_after <= ?
                ORDER BY list_id""",
                    (time.time(),),
                )
            ]
            return tuple(self._read_list(conn, value) for value in ids)

    def mark_synced(self, listing: FilteredList) -> None:
        with transaction(self.storage.path) as conn:
            conn.execute(
                "UPDATE filtered_lists SET synced_revision=?, sync_attempts=0, last_error='' WHERE list_id=?",
                (listing.revision, listing.list_id),
            )

    def fail_sync(self, listing: FilteredList, error: str) -> None:
        with transaction(self.storage.path) as conn:
            conn.execute(
                """UPDATE filtered_lists SET sync_attempts=sync_attempts+1,
                sync_after=?, last_error=? WHERE list_id=? AND revision=?""",
                (time.time() + 5, error[:500], listing.list_id, listing.revision),
            )

    @staticmethod
    def _dirty(conn, list_id: int) -> None:
        conn.execute(
            """UPDATE filtered_lists SET revision=revision+1, sync_attempts=0,
            sync_after=0 WHERE list_id=?""",
            (list_id,),
        )

    @staticmethod
    def _snapshot_article(row) -> DigestArticle:
        fields = {
            key: row[key]
            for key in DigestArticle.__dataclass_fields__
            if key != "related_links"
        }
        return DigestArticle(
            **fields,
            related_links=tuple(
                tuple(v) for v in json.loads(row["related_links_json"])
            ),
        )

    def _read_list(self, conn, list_id: int) -> FilteredList:
        from .storage import FeedbackError

        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM filtered_lists WHERE list_id=?", (list_id,)
        ).fetchone()
        if row is None:
            raise FeedbackError("找不到对应的新闻列表")
        members = conn.execute(
            """SELECT m.member_id, m.snapshot_id, m.verdict, m.reason, m.profile_version,
            CASE WHEN EXISTS(SELECT 1 FROM card_deliveries d WHERE d.article_key=m.article_key)
                THEN 'delivered' ELSE coalesce(r.status, 'available') END AS status,
            s.* FROM filtered_list_members m
            JOIN article_snapshots s USING(snapshot_id)
            LEFT JOIN reveal_requests r USING(member_id)
            WHERE m.list_id=? ORDER BY m.position""",
            (list_id,),
        ).fetchall()
        return FilteredList(
            row["list_id"],
            row["digest_key"],
            row["issue_date"],
            row["page"],
            row["pages"],
            row["send_uuid"],
            row["message_id"],
            row["chat_id"],
            row["revision"],
            tuple(
                ListMember(
                    m["member_id"],
                    m["snapshot_id"],
                    self._snapshot_article(m),
                    PersonalizedEvaluation(m["article_key"], m["verdict"], m["reason"]),
                    m["profile_version"],
                    m["status"],
                )
                for m in members
            ),
        )
