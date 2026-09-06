"""Scout collection, personalization, card delivery, and calibration workflow."""

from __future__ import annotations

import asyncio
import fcntl
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import IO

from .collector import CollectionBatch, CollectionError, collect_source
from .config import AppConfig, FeishuDeliveryConfig, SourceConfig
from .datetime_utils import BEIJING_TIMEZONE, beijing_date, publication_date_bound
from .digest import normalize_articles, parse_issue
from .issue_state import FilteredList, IssueState, ListMember
from .llm import (
    EVALUATION_BATCH_SIZE,
    LLMError,
    ModelConfig,
    PersonalizationLLM,
    resolve_api_key,
)
from .model import DigestArticle, NewsItem, PersonalizedEvaluation, PreferenceProfile
from .notifier import (
    FeishuNotifier,
    build_article_card,
    build_filtered_list_card,
    build_profile_card,
    partition_filtered_members,
)
from .storage import SQLiteStorage


def _now_beijing() -> datetime:
    return datetime.now(BEIJING_TIMEZONE)


class RunLockedError(RuntimeError):
    """Raised when another sender or calibration process owns the lock."""


@dataclass(slots=True)
class RunStats:
    sent: int = 0
    previewed: int = 0
    failed: int = 0
    baseline: int = 0
    skipped: int = 0


def run(
    config: AppConfig,
    *,
    mode: str,
    database_path: str | Path,
    output: IO[str],
    feishu_delivery: FeishuDeliveryConfig | None = None,
    model_config: ModelConfig | None = None,
    collector_factory: Callable[..., object] | None = None,
    notifier_factory: Callable[..., object] = FeishuNotifier,
    clock: Callable[[], datetime] = _now_beijing,
    issue_date: str | None = None,
) -> int:
    if mode not in {"send", "dry-run", "calibrate"}:
        raise ValueError(f"unsupported mode: {mode}")
    if mode in {"send", "calibrate"} and feishu_delivery is None:
        raise ValueError(f"Feishu delivery configuration is required in --{mode} mode")
    if mode in {"send", "dry-run"} and model_config is None:
        raise ValueError(f"model configuration is required in --{mode} mode")
    if issue_date is not None:
        if mode not in {"send", "dry-run"}:
            raise ValueError("--issue-date is only valid with --send or --dry-run")
        if date.fromisoformat(issue_date).isoformat() != issue_date:
            raise ValueError("--issue-date must use YYYY-MM-DD")
    if mode == "send" and feishu_delivery.receive_id_type != "chat_id":
        raise ValueError("personalized delivery requires a Feishu group chat_id")

    lock_context = (
        _sender_lock(database_path) if mode in {"send", "calibrate"} else _null_lock()
    )
    with lock_context:
        return asyncio.run(
            _run(
                config,
                mode=mode,
                database_path=database_path,
                output=output,
                feishu_delivery=feishu_delivery,
                model_config=model_config,
                collector_factory=collector_factory,
                notifier_factory=notifier_factory,
                clock=clock,
                issue_date=issue_date,
            )
        )


async def _run(
    config: AppConfig,
    *,
    mode: str,
    database_path: str | Path,
    output: IO[str],
    feishu_delivery: FeishuDeliveryConfig | None,
    model_config: ModelConfig | None,
    collector_factory: Callable[..., object] | None,
    notifier_factory: Callable[..., object],
    clock: Callable[[], datetime],
    issue_date: str | None,
) -> int:
    storage = SQLiteStorage(database_path)
    stats = RunStats()
    run_date = beijing_date(clock())
    read_only = mode == "dry-run"
    if not read_only:
        storage.initialize()

    dated_items = {}
    if issue_date is not None:
        # Reject an unavailable date before updating preferences or posting a
        # profile notification. An explicit date never falls through to today.
        for source in config.sources:
            dated_items[source.name] = _select_dated_issue(
                source, config, collector_factory, storage, issue_date, output
            )

    notifier = None
    if feishu_delivery is not None:
        notifier = notifier_factory(feishu_delivery, config.network.timeout_seconds)

    llm = None
    profile = PreferenceProfile.empty()
    if model_config is not None:
        llm = PersonalizationLLM(model_config, resolve_api_key(model_config))
        try:
            profile = await _prepare_profile(storage, llm, mode=mode, output=output)
        except Exception:
            await llm.close()
            raise

    try:
        if mode == "send" and profile.version > 0 and not profile.notified:
            assert notifier is not None
            try:
                card = build_profile_card(
                    profile, max_payload_bytes=config.feishu.max_payload_bytes
                )
                sent = notifier.send_card(card)  # type: ignore[attr-defined]
                storage.mark_profile_notified(
                    profile.version,
                    message_id=sent.message_id,
                    chat_id=sent.chat_id,
                )
                profile = storage.active_profile(read_only=True) or profile
                print(
                    f"Preference profile v{profile.version} notification sent.",
                    file=output,
                )
            except Exception as exc:  # noqa: BLE001 - isolate notification boundary
                stats.failed += 1
                print(
                    f"Profile notification failed: {type(exc).__name__}; "
                    "article delivery is deferred.",
                    file=output,
                )
                _print_summary(stats, output)
                return 1

        for source in config.sources:
            if issue_date is not None:
                item, saved_articles = dated_items[source.name]
                await _process_issue(
                    source,
                    item,
                    mode=mode,
                    storage=storage,
                    notifier=notifier,
                    llm=llm,
                    profile=profile,
                    config=config,
                    output=output,
                    stats=stats,
                    saved_articles=saved_articles,
                    selected_date=issue_date,
                )
                continue
            try:
                batch = _collect(source, config, collector_factory)
            except Exception as exc:  # noqa: BLE001 - isolate source collection
                stats.failed += 1
                print(
                    f"Collection failed: {source.name}: {type(exc).__name__}",
                    file=output,
                )
                continue
            if batch.issues:
                stats.failed += len(batch.issues)
                print(
                    f"Collection warnings: {source.name}: {len(batch.issues)}",
                    file=output,
                )
            items = _unique_items(batch.items, stats)
            items.sort(key=_issue_date, reverse=True)
            if mode == "calibrate":
                if not items:
                    stats.failed += 1
                    print(f"Calibration found no issue: {source.name}", file=output)
                    continue
                if not storage.is_source_initialized(source.name):
                    storage.initialize_source_baseline(source.name, items)
                # Calibration intentionally targets only the newest issue.
                # Seal the older RSS window so the following personalized
                # send cannot replay historical daily issues.  A later
                # explicit calibration can still ignore this whole-issue
                # state and send its own snapshot cards.
                storage.record_delivered(items[1:])
                await _process_issue(
                    source,
                    items[0],
                    mode=mode,
                    storage=storage,
                    notifier=notifier,
                    llm=None,
                    profile=profile,
                    config=config,
                    output=output,
                    stats=stats,
                )
                continue

            initialized = storage.is_source_initialized(
                source.name, read_only=read_only
            )
            if not initialized:
                stats.baseline += len(items)
                label = "Baseline preview" if read_only else "Baseline created"
                print(f"{label}: {source.name}: {len(items)} issue(s)", file=output)
                if read_only:
                    for item in items:
                        print(
                            f"  - {item.published_at or '日期未知'} · {item.title}",
                            file=output,
                        )
                elif not batch.issues:
                    storage.initialize_source_baseline(source.name, items)
                continue

            try:
                unseen = storage.unseen(items[:1], read_only=read_only)
            except Exception as exc:  # noqa: BLE001 - isolate source state
                stats.failed += 1
                print(
                    f"State lookup failed: {source.name}: {type(exc).__name__}",
                    file=output,
                )
                continue
            stats.skipped += len(items) - len(unseen)
            selected = unseen[:1]
            if source.max_age_days is not None:
                selected, stale = _partition_by_age(
                    selected,
                    run_date=run_date,
                    max_age_days=source.max_age_days,
                )
                stats.skipped += len(stale)
                if stale:
                    print(
                        f"Skipped {len(stale)} issue(s) older than "
                        f"{source.max_age_days} days.",
                        file=output,
                    )
            for item in selected:
                await _process_issue(
                    source,
                    item,
                    mode=mode,
                    storage=storage,
                    notifier=notifier,
                    llm=llm,
                    profile=profile,
                    config=config,
                    output=output,
                    stats=stats,
                )
    finally:
        if llm is not None:
            await llm.close()

    if not any((stats.sent, stats.previewed, stats.failed, stats.baseline)):
        print("No new items.", file=output)
    _print_summary(stats, output)
    return 1 if stats.failed else 0


async def _prepare_profile(
    storage: SQLiteStorage,
    llm: PersonalizationLLM,
    *,
    mode: str,
    output: IO[str],
) -> PreferenceProfile:
    read_only = mode == "dry-run"
    active = storage.active_profile(read_only=read_only)
    evidence = storage.feedback_evidence(read_only=read_only)
    latest_revision = storage.max_feedback_revision_id(read_only=read_only)
    likes = sum(item.sentiment == "like" for item in evidence)
    dislikes = sum(item.sentiment == "dislike" for item in evidence)
    if active is None:
        update_needed = likes >= 2 and dislikes >= 2
    else:
        update_needed = latest_revision > active.last_feedback_revision_id
    if not update_needed:
        return active or PreferenceProfile.empty()

    next_version = (active.version if active else 0) + 1
    try:
        generated = await llm.summarize_preferences(
            evidence,
            active,
            next_version=next_version,
            last_feedback_revision_id=latest_revision,
        )
    except Exception as exc:
        raise LLMError(
            "preference update failed; the previous profile will not be used "
            f"for this round ({type(exc).__name__})"
        ) from exc
    if read_only:
        print(
            f"Temporary preference profile v{generated.version} generated "
            "for dry-run; SQLite unchanged.",
            file=output,
        )
        return generated
    saved = storage.save_profile(generated)
    print(f"Preference profile v{saved.version} activated.", file=output)
    return saved


async def _process_issue(
    source: SourceConfig,
    item: NewsItem,
    *,
    mode: str,
    storage: SQLiteStorage,
    notifier: object | None,
    llm: PersonalizationLLM | None,
    profile: PreferenceProfile,
    config: AppConfig,
    output: IO[str],
    stats: RunStats,
    saved_articles: tuple[DigestArticle, ...] | None = None,
    selected_date: str | None = None,
) -> None:
    state = IssueState(storage)
    try:
        if saved_articles is None:
            issue = parse_issue(item.content, page_url=item.url)
            articles = normalize_articles(issue, digest_key=item.dedupe_key)
            issue_date = issue.issue_date or _issue_date(item)
        else:
            articles = saved_articles
            issue_date = selected_date or _issue_date(item)
        if not articles:
            raise ValueError("issue overview contains no entries")
    except Exception as exc:  # noqa: BLE001 - isolate malformed issue
        stats.failed += 1
        print(
            f"Digest parsing failed: {source.name}: {item.title}: {type(exc).__name__}",
            file=output,
        )
        return

    if mode == "dry-run":
        snapshots = {
            (article.article_key, article.content_hash): 0 for article in articles
        }
    else:
        try:
            snapshots = storage.save_article_snapshots(articles)
            state.save_issue(item, issue_date, articles)
        except Exception as exc:  # noqa: BLE001 - isolate snapshot transaction
            stats.failed += 1
            print(
                f"Snapshot write failed: {item.title}: {type(exc).__name__}",
                file=output,
            )
            return

    if mode == "calibrate":
        await _deliver_calibration(
            item,
            articles,
            snapshots=snapshots,
            storage=storage,
            notifier=notifier,
            config=config,
            output=output,
            stats=stats,
        )
        return

    existing_lists = state.lists(item.dedupe_key)
    planned_keys = {
        m.article.article_key for listing in existing_lists for m in listing.members
    }
    pending = [
        article
        for article in articles
        if not state.presented(article.article_key)
        and article.article_key not in planned_keys
    ]
    stats.skipped += len(articles) - len(pending)
    assert llm is not None
    recent_likes = storage.recent_feedback("like", read_only=mode == "dry-run")
    recent_dislikes = storage.recent_feedback("dislike", read_only=mode == "dry-run")
    rejected: list[ListMember] = []
    delivery_blocked = False
    for start in range(0, len(pending), EVALUATION_BATCH_SIZE):
        batch = pending[start : start + EVALUATION_BATCH_SIZE]
        evaluations: dict[str, PersonalizedEvaluation] = {}
        missing: list[DigestArticle] = []
        for article in batch:
            cached = storage.cached_evaluation(
                article, profile.version, read_only=mode == "dry-run"
            )
            if cached is None:
                missing.append(article)
            else:
                evaluations[article.article_key] = cached
        try:
            if missing:
                generated = await llm.evaluate_articles(
                    missing, profile, recent_likes, recent_dislikes
                )
                evaluations.update(
                    (evaluation.article_key, evaluation) for evaluation in generated
                )
                if mode == "send":
                    storage.save_evaluations(
                        missing, generated, profile_version=profile.version
                    )
        except Exception as exc:  # noqa: BLE001 - isolate one model batch
            stats.failed += len(batch)
            print(
                f"Evaluation batch failed: {item.title}: positions "
                f"{batch[0].position}-{batch[-1].position}: {type(exc).__name__}",
                file=output,
            )
            delivery_blocked = True
            continue
        for article in batch:
            evaluation = evaluations[article.article_key]
            snapshot_id = snapshots[(article.article_key, article.content_hash)]
            if evaluation.verdict == "不推荐":
                rejected.append(
                    ListMember(
                        snapshot_id or article.position,
                        snapshot_id,
                        article,
                        evaluation,
                        profile.version,
                    )
                )
                continue
            if delivery_blocked:
                continue
            try:
                card = build_article_card(
                    article,
                    evaluation,
                    snapshot_id=snapshot_id,
                    purpose="personalized",
                    max_payload_bytes=config.feishu.max_payload_bytes,
                )
                if mode == "dry-run":
                    print(
                        json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                        file=output,
                    )
                    stats.previewed += 1
                    continue
                assert notifier is not None
                sent = notifier.send_card(card)  # type: ignore[attr-defined]
                storage.record_card_delivery(
                    snapshot_id=snapshot_id,
                    article_key=article.article_key,
                    purpose="personalized",
                    message_id=sent.message_id,
                    chat_id=sent.chat_id,
                    evaluation=evaluation,
                )
                stats.sent += 1
            except Exception as exc:  # noqa: BLE001 - isolate one card delivery
                stats.failed += 1
                delivery_blocked = True
                print(
                    f"Card delivery failed: {item.title} #{article.position}: "
                    f"{type(exc).__name__}",
                    file=output,
                )

    # Freeze and present lists only after every evaluation and ordinary body
    # succeeded. Keep cached judgments and any partial list plan for retries.
    if delivery_blocked:
        print(
            f"Tail list deferred until missing bodies/evaluations recover: {issue_date}",
            file=output,
        )
        return
    try:
        if rejected and existing_lists:
            raise ValueError(
                "issue gained new rejected entries after its list was frozen"
            )
        pages = partition_filtered_members(
            rejected,
            issue_date=issue_date,
            max_payload_bytes=config.feishu.max_payload_bytes,
        )
        if mode == "dry-run":
            previews = existing_lists or tuple(
                FilteredList(
                    0,
                    item.dedupe_key,
                    issue_date,
                    index,
                    len(pages),
                    "",
                    None,
                    "",
                    0,
                    members,
                )
                for index, members in enumerate(pages, 1)
            )
            for listing in previews:
                if listing.message_id:
                    continue
                print(
                    json.dumps(
                        build_filtered_list_card(
                            listing, max_payload_bytes=config.feishu.max_payload_bytes
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    file=output,
                )
                stats.previewed += 1
        else:
            assert notifier is not None
            listings = existing_lists or state.create_lists(
                digest_key=item.dedupe_key,
                issue_date=issue_date,
                chat_id=notifier.delivery.receive_id,
                pages=pages,
            )
            for listing in listings:
                if listing.message_id:
                    continue
                card = build_filtered_list_card(
                    listing, max_payload_bytes=config.feishu.max_payload_bytes
                )
                sent = notifier.send_card(
                    card, send_uuid=listing.send_uuid, chat_id=listing.chat_id
                )
                state.mark_list_sent(listing.list_id, sent.message_id, sent.chat_id)
                stats.sent += 1
                print(
                    f"Tail list sent: {issue_date}: {len(listing.members)} entries "
                    f"({listing.page}/{listing.pages})",
                    file=output,
                )
    except Exception as exc:  # noqa: BLE001 - isolate tail delivery and accounting
        stats.failed += 1
        print(f"Tail list failed: {issue_date}: {exc}", file=output)
        return

    if mode == "send" and all(
        state.presented(article.article_key) for article in articles
    ):
        try:
            storage.record_delivered([item])
        except Exception as exc:  # noqa: BLE001 - isolate completion accounting
            stats.failed += 1
            print(
                f"Issue completion write failed: {item.title}: {type(exc).__name__}",
                file=output,
            )


async def _deliver_calibration(
    item: NewsItem,
    articles: Sequence[DigestArticle],
    *,
    snapshots: dict[tuple[str, str], int],
    storage: SQLiteStorage,
    notifier: object | None,
    config: AppConfig,
    output: IO[str],
    stats: RunStats,
) -> None:
    for article in articles:
        snapshot_id = snapshots[(article.article_key, article.content_hash)]
        if storage.is_snapshot_delivered(snapshot_id, "calibration"):
            stats.skipped += 1
            continue
        evaluation = PersonalizedEvaluation(
            article.article_key,
            "不确定",
            "校准阶段暂不调用模型；请用喜欢或不喜欢及具体原因帮助建立偏好档案。",
        )
        try:
            card = build_article_card(
                article,
                evaluation,
                snapshot_id=snapshot_id,
                purpose="calibration",
                max_payload_bytes=config.feishu.max_payload_bytes,
            )
            assert notifier is not None
            sent = notifier.send_card(card)  # type: ignore[attr-defined]
            storage.record_card_delivery(
                snapshot_id=snapshot_id,
                article_key=article.article_key,
                purpose="calibration",
                message_id=sent.message_id,
                chat_id=sent.chat_id,
                evaluation=evaluation,
            )
            stats.sent += 1
        except Exception as exc:  # noqa: BLE001 - isolate one calibration card
            stats.failed += 1
            print(
                f"Calibration card failed: {item.title} #{article.position}: "
                f"{type(exc).__name__}",
                file=output,
            )
    if all(
        storage.is_snapshot_delivered(
            snapshots[(article.article_key, article.content_hash)], "calibration"
        )
        for article in articles
    ):
        try:
            storage.record_delivered([item])
        except Exception as exc:  # noqa: BLE001 - isolate completion accounting
            stats.failed += 1
            print(
                f"Calibration completion write failed: {type(exc).__name__}",
                file=output,
            )


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
    return batch


def _issue_date(item: NewsItem) -> str:
    issue_date = parse_issue(item.content, page_url=item.url).issue_date
    if issue_date:
        return date.fromisoformat(issue_date).isoformat()
    bound = publication_date_bound(item.published_at)
    if bound is None:
        raise ValueError(f"issue has no identifiable date: {item.title}")
    return bound.isoformat()


def _select_dated_issue(
    source: SourceConfig,
    config: AppConfig,
    collector_factory: Callable[..., object] | None,
    storage: SQLiteStorage,
    issue_date: str,
    output: IO[str],
) -> tuple[NewsItem, tuple[DigestArticle, ...] | None]:
    try:
        batch = _collect(source, config, collector_factory)
    except Exception as exc:  # noqa: BLE001 - permit full-snapshot fallback
        print(
            f"RSS unavailable ({type(exc).__name__}); looking for full issue snapshot.",
            file=output,
        )
    else:
        matches = {
            item.dedupe_key: item
            for item in batch.items
            if _issue_date(item) == issue_date
        }
        if len(matches) > 1:
            raise ValueError(f"multiple RSS issues match {issue_date}")
        if matches:
            item = next(iter(matches.values()))
            print(f"Selected issue from RSS: {issue_date}", file=output)
            return item, None
    saved = IssueState(storage).load_issue(source.name, issue_date)
    if saved is None:
        raise ValueError(f"{issue_date} not found in RSS or a complete saved snapshot")
    print(f"Selected complete saved snapshot: {issue_date}", file=output)
    return saved


def _unique_items(items: Sequence[NewsItem], stats: RunStats) -> list[NewsItem]:
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
    cutoff = date.fromisoformat(run_date) - timedelta(days=max_age_days)
    fresh: list[NewsItem] = []
    stale: list[NewsItem] = []
    for item in items:
        bound = publication_date_bound(item.published_at)
        (stale if bound is not None and bound < cutoff else fresh).append(item)
    return fresh, stale


def _print_summary(stats: RunStats, output: IO[str]) -> None:
    print(
        "Summary: "
        f"sent={stats.sent} failed={stats.failed} baseline={stats.baseline} "
        f"skipped={stats.skipped} previewed={stats.previewed}",
        file=output,
    )


@contextmanager
def _sender_lock(database_path: str | Path) -> Iterator[None]:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.sender.lock")
    with lock_path.open("a+b") as file:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunLockedError("another Scout sender is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _null_lock() -> Iterator[None]:
    yield
