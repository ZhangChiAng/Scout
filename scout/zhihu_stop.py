"""Discard a stopped scan after its collector, model and sender have drained."""

import copy
import json

from . import zhihu_store
from .config import load_config
from .database import transaction
from .locking import sender_lock
from .zhihu_cards import build_stream_card


def finish_stop(database, state):
    """Caller owns the scan lock; commit cleanup and its final card together."""
    from .zhihu_stream import export_report, refresh

    with sender_lock(database):
        refresh(database, state)
        cleaned = copy.deepcopy(state)
        with transaction(database, rows=True) as conn:
            saved = json.loads(
                conn.execute(
                    "SELECT state_json FROM zhihu_scans WHERE id=?", (state["id"],)
                ).fetchone()[0]
            )
            if saved.get("stop_cleanup"):
                state.clear()
                state.update(saved)
                return
            scan_id = state["id"]
            # Preserve evidence for completed or possibly completed HTTP sends.
            # An attempted request with no success response cannot prove failure.
            conn.execute(
                "CREATE TEMP TABLE stop_kept_keys (content_key TEXT PRIMARY KEY, delivered INTEGER NOT NULL)"
            )
            conn.execute(
                """INSERT OR IGNORE INTO stop_kept_keys
                SELECT content_key,max(delivered) FROM (
                    SELECT article_key AS content_key,status='delivered' AS delivered
                    FROM zhihu_link_deliveries
                    WHERE scan_id=? AND (status='delivered' OR attempts>0)
                    UNION ALL SELECT s.content_key,d.status='delivered'
                    FROM zhihu_card_deliveries d
                    JOIN zhihu_content_snapshots s ON s.snapshot_id=d.snapshot_id
                    WHERE s.scan_id=? AND (d.status='delivered' OR d.attempts>0))
                GROUP BY content_key""",
                (scan_id, scan_id),
            )
            kept = {
                r[0] for r in conn.execute("SELECT content_key FROM stop_kept_keys")
            }
            candidates = set(state["candidates"])
            candidates.update(
                r[0]
                for r in conn.execute(
                    """SELECT content_key FROM zhihu_candidate_discoveries WHERE scan_id=?
                    UNION SELECT content_key FROM zhihu_candidate_results WHERE scan_id=?""",
                    (scan_id, scan_id),
                )
            )
            # Limit orphan collection to this scan, including snapshots saved
            # just before a crash prevented insertion of the candidate result.
            conn.execute(
                "CREATE TEMP TABLE stop_snapshots (snapshot_id INTEGER PRIMARY KEY)"
            )
            conn.execute(
                """INSERT OR IGNORE INTO stop_snapshots
                SELECT snapshot_id FROM zhihu_content_snapshots WHERE scan_id=?
                UNION SELECT snapshot_id FROM zhihu_candidate_results
                WHERE scan_id=? AND snapshot_id IS NOT NULL""",
                (scan_id, scan_id),
            )
            conn.execute(
                """DELETE FROM zhihu_settings WHERE key LIKE 'review_event:%'
                AND CAST(value_json AS INTEGER) IN (
                    SELECT position FROM zhihu_link_deliveries
                    WHERE scan_id=? AND status!='delivered' AND attempts=0)""",
                (scan_id,),
            )
            conn.execute(
                """DELETE FROM zhihu_link_deliveries
                WHERE scan_id=? AND status!='delivered' AND attempts=0""",
                (scan_id,),
            )
            conn.execute(
                """UPDATE zhihu_link_deliveries SET status='failed',
                card_json=NULL,retry_at=0,last_error='stopped_delivery_unknown'
                WHERE scan_id=? AND status!='delivered'""",
                (scan_id,),
            )
            conn.execute(
                """DELETE FROM zhihu_card_deliveries
                WHERE status!='delivered' AND (
                    event_id LIKE ? OR (attempts=0 AND snapshot_id IN (
                        SELECT snapshot_id FROM zhihu_content_snapshots WHERE scan_id=?)))""",
                (f"stream:{scan_id}:%", scan_id),
            )
            conn.execute(
                """UPDATE zhihu_card_deliveries SET status='failed',
                card_json='{}',retry_at=0,last_error='stopped_delivery_unknown'
                WHERE status!='delivered' AND snapshot_id IN (
                    SELECT snapshot_id FROM zhihu_content_snapshots WHERE scan_id=?)""",
                (scan_id,),
            )
            unknown = conn.execute(
                """SELECT count(DISTINCT content_key) FROM (
                    SELECT article_key AS content_key FROM zhihu_link_deliveries
                    WHERE scan_id=? AND last_error='stopped_delivery_unknown'
                    UNION SELECT s.content_key FROM zhihu_card_deliveries d
                    JOIN zhihu_content_snapshots s ON s.snapshot_id=d.snapshot_id
                    WHERE s.scan_id=? AND d.last_error='stopped_delivery_unknown')""",
                (scan_id, scan_id),
            ).fetchone()[0]
            for table in ("zhihu_candidate_results", "zhihu_candidate_discoveries"):
                conn.execute(
                    f"DELETE FROM {table} WHERE scan_id=? "
                    "AND content_key NOT IN (SELECT content_key FROM stop_kept_keys WHERE delivered=1)",
                    (scan_id,),
                )
            conn.execute(
                "DELETE FROM zhihu_question_expansions WHERE scan_id=?", (scan_id,)
            )
            conn.execute(
                """DELETE FROM zhihu_content_snapshots AS s
                WHERE snapshot_id IN (SELECT snapshot_id FROM stop_snapshots)
                AND NOT EXISTS (SELECT 1 FROM zhihu_candidate_results r WHERE r.snapshot_id=s.snapshot_id)
                AND NOT EXISTS (SELECT 1 FROM zhihu_feedback_revisions f WHERE f.snapshot_id=s.snapshot_id)
                AND NOT EXISTS (SELECT 1 FROM zhihu_link_deliveries d WHERE d.snapshot_id=s.snapshot_id)
                AND NOT EXISTS (SELECT 1 FROM zhihu_card_deliveries d WHERE d.snapshot_id=s.snapshot_id)
                AND NOT EXISTS (
                    SELECT 1 FROM zhihu_scans scans, json_each(scans.state_json,'$.candidates') c
                    WHERE scans.id!=? AND (
                        json_extract(c.value,'$.snapshot_id')=s.snapshot_id
                        OR json_extract(c.value,'$.article.snapshot_id')=s.snapshot_id))""",
                (scan_id,),
            )
            stamp = zhihu_store._now()
            cleaned.update(
                status="stopped",
                stop_requested=True,
                stop_reason="user_stop",
                last_error=None,
                stop_cleanup={
                    "completed_at": stamp,
                    "discarded_count": len(candidates - kept),
                    "unknown_deliveries": unknown,
                },
                candidates={},
                active=None,
                operations=[],
                queries=[],
                controls=[],
                errors=[],
                requests=[],
            )
            # The immutable snapshots retain delivered bodies and feedback
            # context. Keep only aggregate usage and counts in the scan journal.
            cleaned["stream"].update(keys=[], results={})
            cleaned["stream"]["pipeline"]["active"] = {"content": 0}
            cleaned["candidate_counts"]["pending"] = 0
            cleaned["request"].update(
                pending=0, failed=0, reserved=cleaned["request"]["qualified"]
            )
            conn.execute(
                """UPDATE zhihu_jobs SET
                status=CASE WHEN json_extract(payload_json,'$.action')='stop'
                    THEN 'completed' ELSE 'cancelled' END, updated_at=?
                WHERE kind='semantic_control' AND status='pending'
                AND json_extract(payload_json,'$.scan_id')=?""",
                (stamp, scan_id),
            )
            conn.execute(
                "UPDATE zhihu_scans SET status='stopped',state_json=? WHERE id=?",
                (zhihu_store._json(cleaned), scan_id),
            )
            suffix = f"{cleaned['request']['id']}:{cleaned.get('resume_sequence', 0)}:stopped"
            zhihu_store.enqueue_card(
                database,
                build_stream_card(
                    cleaned, max_payload_bytes=load_config().feishu.max_payload_bytes
                ),
                cleaned["chat_id"],
                event_id=f"stream:{scan_id}:{suffix}",
                connection=conn,
            )
            # Write the compact export before committing the terminal marker.
            # A crash or file error leaves cleanup eligible for another attempt.
            export_report(cleaned)
        state.clear()
        state.update(cleaned)
