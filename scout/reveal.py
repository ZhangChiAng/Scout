"""One listener worker sends durable body requests without calling the model."""

from __future__ import annotations

import fcntl
import logging
import threading

from .issue_state import IssueState
from .notifier import FeishuNotifier, build_article_card, build_filtered_list_card

logger = logging.getLogger(__name__)


class RevealWorker:
    def __init__(
        self, state: IssueState, notifier: FeishuNotifier, *, max_payload_bytes: int
    ) -> None:
        self.state = state
        self.notifier = notifier
        self.max_payload_bytes = max_payload_bytes
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="scout-reveal", daemon=True
        )
        self._lock = None

    def start(self) -> None:
        path = self.state.storage.path
        self._lock = path.with_name(f"{path.name}.listener.lock").open("a+b")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.state.recover()
        except BaseException:
            self._lock.close()
            self._lock = None
            raise
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join()
        if self._lock is not None:
            self._lock.close()

    def _sync_cards(self) -> None:
        for listing in self.state.dirty_lists():
            try:
                card = build_filtered_list_card(
                    listing, max_payload_bytes=self.max_payload_bytes
                )
                self.notifier.update_card(listing.message_id, card)
                self.state.mark_synced(listing)
            except Exception as exc:  # noqa: BLE001 - preserve durable patch retry
                self.state.fail_sync(listing, str(exc))
                logger.warning("List %s update failed: %s", listing.list_id, exc)

    def _run(self) -> None:
        logger.info("Reveal worker started; durable requests recovered")
        needs_recovery = False
        while not self.stop.is_set():
            self.wake.clear()
            try:
                if needs_recovery:
                    # A database failure while recording success/failure must
                    # not strand a claimed request until the next restart.
                    self.state.recover()
                    needs_recovery = False
                self._sync_cards()
                request = self.state.claim()
                if request is not None:
                    try:
                        if not self.state.reconcile_delivery(request):
                            m = request.member
                            card = build_article_card(
                                m.article,
                                m.evaluation,
                                snapshot_id=m.snapshot_id,
                                purpose="personalized",
                                max_payload_bytes=self.max_payload_bytes,
                            )
                            sent = self.notifier.send_card(
                                card,
                                send_uuid=request.send_uuid,
                                chat_id=request.chat_id,
                            )
                            self.state.finish(
                                request,
                                message_id=sent.message_id,
                                chat_id=sent.chat_id,
                            )
                            logger.info(
                                "Revealed list=%s member=%s attempt=%s",
                                request.list_id,
                                m.member_id,
                                request.attempts,
                            )
                    except Exception as exc:  # noqa: BLE001 - preserve durable body retry
                        self.state.fail(request, str(exc))
                        logger.warning(
                            "Reveal failed list=%s member=%s attempt=%s: %s",
                            request.list_id,
                            request.member.member_id,
                            request.attempts,
                            exc,
                        )
                    continue
            except Exception:
                needs_recovery = True
                logger.exception("Reveal worker iteration failed")
            # Wake immediately on a callback; polling also recovers requests
            # committed just before process termination or a lost wake signal.
            self.wake.wait(1)
