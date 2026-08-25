"""Scout single-source application orchestration."""

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

from .collector import (
    CollectionBatch,
    CollectionError,
    CollectionIssue,
    collect_source,
)
from .config import AppConfig, FeishuDeliveryConfig, SourceConfig
from .datetime_utils import BEIJING_TIMEZONE, beijing_date, publication_date_bound
from .digest import parse_issue
from .model import NewsItem, canonicalize_url
from .notifier import (
    FeishuNotifier,
    build_failure_digest,
    build_issue_digest,
)
from .storage import (
    AGE_FAILURE_ITEM_KEY,
    BASELINE_FAILURE_ITEM_KEY,
    RECONCILE_FAILURE_ITEM_KEY,
    SOURCE_FAILURE_ITEM_KEY,
    STATE_FAILURE_ITEM_KEY,
    UNSEEN_FAILURE_ITEM_KEY,
    SQLiteStorage,
)

LOGGER = logging.getLogger(__name__)


def _now_beijing() -> datetime:
    return datetime.now(BEIJING_TIMEZONE)


@dataclass(frozen=True, slots=True)
class _Candidate:
    source: SourceConfig
    item: NewsItem


@dataclass(frozen=True, slots=True)
class _FailureEvent:
    source: str
    item_key: str
    article_title: str
    stage: str


@dataclass(slots=True)
class _RunStats:
    sent: int = 0
    previewed: int = 0
    failed: int = 0
    baseline: int = 0
    skipped: int = 0
    failure_events: set[tuple[str, str]] = field(default_factory=set)


@dataclass(slots=True)
class _LazyNotifier:
    factory: Callable[..., object]
    delivery: FeishuDeliveryConfig
    timeout_seconds: float
    instance: object | None = None
    initialization_error: Exception | None = None
    attempted: bool = False

    def send(self, digest: object) -> None:
        if not self.attempted:
            self.attempted = True
            try:
                self.instance = self.factory(self.delivery, self.timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - isolate notifier boundary
                self.initialization_error = exc
        if self.initialization_error is not None:
            raise self.initialization_error
        if self.instance is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("notifier initialization returned no instance")
        self.instance.send(digest)  # type: ignore[attr-defined]


def run(
    config: AppConfig,
    *,
    mode: str,
    database_path: str | Path,
    output: TextIO,
    feishu_delivery: FeishuDeliveryConfig | None = None,
    collector_factory: Callable[..., object] | None = None,
    notifier_factory: Callable[..., object] = FeishuNotifier,
    clock: Callable[[], datetime] = _now_beijing,
) -> int:
    """Run one collection round.

    Collection, baseline establishment, and delivery follow configuration
    order.  Every delivered issue is persisted independently, failures are
    isolated per event with a one-shot alert, and the exit code is 1 when
    anything failed.
    """

    if mode not in {"dry-run", "send"}:
        raise ValueError(f"unsupported mode: {mode}")
    if mode == "send" and feishu_delivery is None:
        raise ValueError("Feishu delivery configuration is required in --send mode")

    run_date = beijing_date(clock())
    storage = SQLiteStorage(database_path)
    notifier = None
    if mode == "send":
        # Construction is lazy: a baseline-only run neither needs nor touches
        # Feishu, while a broken client is isolated like any other send failure.
        assert feishu_delivery is not None
        notifier = _LazyNotifier(
            notifier_factory, feishu_delivery, config.network.timeout_seconds
        )
    stats = _RunStats()
    candidates: list[_Candidate] = []

    for source in config.sources:
        try:
            batch = _collect(source, config, collector_factory)
        except Exception as exc:  # noqa: BLE001 - isolate one source collector
            LOGGER.warning("source collection failed: %s: %s", source.name, exc)
            _report_failure(
                _FailureEvent(
                    source.name,
                    SOURCE_FAILURE_ITEM_KEY,
                    "（来源采集）",
                    "collection",
                ),
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue

        issue_events = tuple(_issue_event(source, issue) for issue in batch.issues)
        if mode == "send":
            # A successful source-level collection is the recovery boundary for
            # the source event.  Article events remain active until delivery.
            try:
                storage.clear_active_failure(source.name, SOURCE_FAILURE_ITEM_KEY)
                storage.reconcile_active_failure_namespace(
                    source.name,
                    "collection:",
                    {
                        event.item_key
                        for event in issue_events
                        if event.item_key.startswith("collection:")
                    },
                )
                storage.clear_active_failure(source.name, RECONCILE_FAILURE_ITEM_KEY)
            except Exception:  # noqa: BLE001 - isolate state reconciliation
                _report_failure(
                    _FailureEvent(
                        source.name,
                        RECONCILE_FAILURE_ITEM_KEY,
                        "（来源状态）",
                        "state",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )

        for event in issue_events:
            _report_failure(
                event,
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )

        source_items = _unique_source_items(batch.items, stats)
        try:
            initialized = storage.is_source_initialized(
                source.name, read_only=(mode == "dry-run")
            )
        except Exception:  # noqa: BLE001 - isolate one source state lookup
            _report_failure(
                _FailureEvent(
                    source.name,
                    STATE_FAILURE_ITEM_KEY,
                    "（来源状态）",
                    "state",
                ),
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue
        if mode == "send":
            try:
                storage.clear_active_failure(source.name, STATE_FAILURE_ITEM_KEY)
            except Exception:  # noqa: BLE001 - isolate recovery accounting
                _report_failure(
                    _FailureEvent(
                        source.name,
                        STATE_FAILURE_ITEM_KEY,
                        "（来源状态）",
                        "state",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
        if not initialized:
            if batch.issues:
                print(
                    f"Baseline deferred: {source.name}: "
                    f"{len(batch.issues)} collection issue(s)",
                    file=output,
                )
                continue
            if mode == "dry-run":
                stats.baseline += len(source_items)
                _print_baseline_preview(source, source_items, output, dry_run=True)
                continue
            try:
                created = storage.initialize_source_baseline(source.name, source_items)
            except Exception:  # noqa: BLE001 - isolate baseline transaction
                _report_failure(
                    _FailureEvent(
                        source.name,
                        BASELINE_FAILURE_ITEM_KEY,
                        "（首次基线）",
                        "baseline",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
                continue
            try:
                storage.clear_active_failure(source.name, BASELINE_FAILURE_ITEM_KEY)
            except Exception:  # noqa: BLE001 - isolate recovery accounting
                _report_failure(
                    _FailureEvent(
                        source.name,
                        BASELINE_FAILURE_ITEM_KEY,
                        "（首次基线）",
                        "baseline",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
            if created:
                stats.baseline += len(source_items)
                _print_baseline_preview(source, source_items, output, dry_run=False)
                continue

        try:
            unseen = storage.unseen(source_items, read_only=(mode == "dry-run"))
        except Exception:  # noqa: BLE001 - isolate one source state query
            _report_failure(
                _FailureEvent(
                    source.name,
                    UNSEEN_FAILURE_ITEM_KEY,
                    "（来源状态）",
                    "state",
                ),
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue
        if mode == "send":
            try:
                storage.clear_active_failure(source.name, UNSEEN_FAILURE_ITEM_KEY)
            except Exception:  # noqa: BLE001 - isolate recovery accounting
                _report_failure(
                    _FailureEvent(
                        source.name,
                        UNSEEN_FAILURE_ITEM_KEY,
                        "（来源状态）",
                        "state",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
        stats.skipped += len(source_items) - len(unseen)
        selected = unseen
        if source.max_age_days is not None:
            fresh, stale = _partition_by_age(
                selected, run_date=run_date, max_age_days=source.max_age_days
            )
            if stale:
                stats.skipped += len(stale)
                _report_failure(
                    _FailureEvent(
                        source.name,
                        AGE_FAILURE_ITEM_KEY,
                        _age_alert_title(stale, source.max_age_days),
                        "age",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
                selected = fresh
            elif mode == "send":
                # A round with no stale articles is the recovery boundary for
                # the aggregated age alert.
                try:
                    storage.clear_active_failure(source.name, AGE_FAILURE_ITEM_KEY)
                except Exception:  # noqa: BLE001 - isolate recovery accounting
                    _report_failure(
                        _FailureEvent(
                            source.name,
                            AGE_FAILURE_ITEM_KEY,
                            "（超龄恢复状态）",
                            "state",
                        ),
                        mode=mode,
                        notifier=notifier,
                        storage=storage,
                        config=config,
                        run_date=run_date,
                        output=output,
                        stats=stats,
                    )
        candidates.extend(_Candidate(source, item) for item in selected)

    for candidate in candidates:
        try:
            issue = parse_issue(candidate.item.content, page_url=candidate.item.url)
        except Exception:  # noqa: BLE001 - isolate one digest parse
            _report_item_failure(
                candidate,
                "digest",
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue
        try:
            issue_date = issue.issue_date or run_date
            digest = build_issue_digest(
                issue,
                items=(candidate.item,),
                title=f"{issue_date} · Scout · {candidate.source.name}",
                max_payload_bytes=config.feishu.max_payload_bytes,
            )
        except Exception:  # noqa: BLE001 - isolate message construction
            _report_item_failure(
                candidate,
                "message",
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue

        if mode == "dry-run":
            print(digest.encoded.decode("utf-8"), file=output)
            stats.previewed += 1
            continue

        try:
            notifier.send(digest)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - isolate remote delivery
            _report_item_failure(
                candidate,
                "send",
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue

        stats.sent += 1
        try:
            storage.record_delivered([candidate.item])
        except Exception:  # noqa: BLE001 - isolate delivery accounting
            # The remote delivery succeeded, but without durable accounting the
            # round must still fail.  A compact alert is attempted just like any
            # other article failure; it is deliberately not retried recursively.
            _report_item_failure(
                candidate,
                "record",
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue

    if not candidates and not stats.failed and not stats.baseline:
        print("No new items.", file=output)
    print(
        "Summary: "
        f"sent={stats.sent} failed={stats.failed} baseline={stats.baseline} "
        f"skipped={stats.skipped} previewed={stats.previewed}",
        file=output,
    )
    return 1 if stats.failed else 0


def _collect(
    source: SourceConfig,
    config: AppConfig,
    collector_factory: Callable[..., object] | None,
) -> CollectionBatch:
    if collector_factory is None:
        batch = collect_source(source, config.network)
    else:
        collector_or_result = collector_factory(source, config.network)
        result = (
            collector_or_result.collect()  # type: ignore[attr-defined]
            if hasattr(collector_or_result, "collect")
            else collector_or_result
        )
        if isinstance(result, CollectionBatch):
            batch = result
        elif isinstance(result, Sequence) and not isinstance(result, str | bytes):
            batch = CollectionBatch(items=tuple(result), issues=())
        else:
            raise CollectionError("collector returned an unsupported result")
    if any(not isinstance(item, NewsItem) for item in batch.items):
        raise CollectionError("collector returned a non-NewsItem entry")
    if any(item.source != source.name for item in batch.items):
        raise CollectionError("collector returned an entry for the wrong source")
    if any(not isinstance(issue, CollectionIssue) for issue in batch.issues):
        raise CollectionError("collector returned an invalid CollectionIssue")
    return batch


def _unique_source_items(items: Sequence[NewsItem], stats: _RunStats) -> list[NewsItem]:
    result: list[NewsItem] = []
    seen: set[str] = set()
    for item in items:
        if item.dedupe_key in seen:
            stats.skipped += 1
            continue
        seen.add(item.dedupe_key)
        result.append(item)
    return result


def _partition_by_age(
    items: Sequence[NewsItem], *, run_date: str, max_age_days: int
) -> tuple[list[NewsItem], list[NewsItem]]:
    """Split candidates into fresh items and ones published too long ago.

    Age uses the latest possible Beijing publication day for the item's
    precision; month-only dates expire with their whole month and unparseable
    dates stay fresh rather than dropping legitimate coverage.
    """

    cutoff = date.fromisoformat(run_date) - timedelta(days=max_age_days)
    fresh: list[NewsItem] = []
    stale: list[NewsItem] = []
    for item in items:
        bound = publication_date_bound(item.published_at)
        if bound is not None and bound < cutoff:
            stale.append(item)
        else:
            fresh.append(item)
    return fresh, stale


def _age_alert_title(stale: Sequence[NewsItem], max_age_days: int) -> str:
    oldest = min(
        (publication_date_bound(item.published_at) for item in stale),
        key=lambda bound: bound or date.max,
    )
    oldest_label = oldest.isoformat() if oldest is not None else "日期未知"
    sample = stale[0].title
    return (
        f"{len(stale)} 篇超过 {max_age_days} 天的文章已跳过"
        f"（最老 {oldest_label}，如：{sample[:80]}）"
    )


def _print_baseline_preview(
    source: SourceConfig,
    items: Sequence[NewsItem],
    output: TextIO,
    *,
    dry_run: bool,
) -> None:
    label = "Baseline preview" if dry_run else "Baseline created"
    print(f"{label}: {source.name}: {len(items)} item(s)", file=output)
    if dry_run:
        for item in items:
            date = item.published_at or "日期未知"
            print(f"  - {date} · {item.title}", file=output)


def _report_item_failure(
    candidate: _Candidate,
    stage: str,
    **kwargs: object,
) -> None:
    _report_failure(
        _FailureEvent(
            candidate.source.name,
            candidate.item.dedupe_key,
            candidate.item.title,
            stage,
        ),
        **kwargs,  # type: ignore[arg-type]
    )


def _issue_event(source: SourceConfig, issue: CollectionIssue) -> _FailureEvent:
    title = issue.title or f"（列表条目 {issue.index or '?'}）"
    issue_url = canonicalize_url(issue.url) if issue.url else ""
    if issue_url and issue_url != canonicalize_url(source.url):
        # A recognizable article keeps one identity while it moves from parse
        # to digest, message, or delivery failure.
        item_key = issue_url
    else:
        stable_label = " ".join(issue.title.split()).casefold()
        if not stable_label:
            stable_label = f"index:{issue.index or '?'}"
        identity = f"{source.name}\0{canonicalize_url(source.url)}\0{stable_label}"
        item_key = "collection:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _FailureEvent(
        source.name,
        item_key,
        title,
        issue.stage or "collection",
    )


def _report_failure(
    event: _FailureEvent,
    *,
    mode: str,
    notifier: object | None,
    storage: SQLiteStorage,
    config: AppConfig,
    run_date: str,
    output: TextIO,
    stats: _RunStats,
) -> None:
    identity = (event.source, event.item_key)
    if identity in stats.failure_events:
        return
    stats.failure_events.add(identity)
    stats.failed += 1
    print(
        f"Failure: source={event.source} article={event.article_title} "
        f"stage={event.stage}",
        file=output,
    )
    if mode != "send" or notifier is None:
        return
    try:
        already_active = storage.has_active_failure(event.source, event.item_key)
    except Exception:  # noqa: BLE001 - alert state must not abort the round
        already_active = False
    if already_active:
        return
    try:
        digest = build_failure_digest(
            title=f"{run_date} · Scout · 告警",
            source=event.source,
            article_title=event.article_title,
            stage=event.stage,
            max_payload_bytes=config.feishu.max_payload_bytes,
        )
        notifier.send(digest)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - alerts are best effort
        return
    try:
        storage.record_active_failure(event.source, event.item_key)
    except Exception:  # noqa: BLE001 - alert accounting is best effort
        # The alert was delivered but cannot be durably deduplicated.  Retrying
        # next run is safer than aborting unrelated items.
        return
