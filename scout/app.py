"""Scout collection, personalization, card delivery, and calibration workflow."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import IO

from .collector import CollectionBatch, collect_source
from .config import AppConfig, FeishuDeliveryConfig, SourceConfig
from .datetime_utils import (
    BEIJING_TIMEZONE,
    beijing_date,
    publication_date_bound,
)
from .digest import issue_date_for
from .issue_delivery import prepare_issue, process_issue, save_issue
from .issue_state import IssueState
from .llm import (
    ModelConfig,
    PersonalizationLLM,
    resolve_api_key,
)
from .locking import sender_lock
from .model import DigestArticle, NewsItem, PreferenceProfile, ProfileSnapshot, RunStats
from .notifier import FeishuNotifier
from .profile_flow import resolve_profile
from .storage import SQLiteStorage


def _now_beijing() -> datetime:
    return datetime.now(BEIJING_TIMEZONE)


def run(
    config: AppConfig,
    *,
    mode: str,
    database_path: str | Path,
    output: IO[str],
    feishu_delivery: FeishuDeliveryConfig | None = None,
    model_config: ModelConfig | None = None,
    issue_date: str | None = None,
    scheduled: bool = False,
) -> int:
    profile_modes = {"profile-update", "profile-rebuild", "profile-rebuild-preview"}
    if mode not in {"send", "dry-run", "calibrate", *profile_modes}:
        raise ValueError(f"unsupported mode: {mode}")
    if (
        mode in {"send", "calibrate", "profile-rebuild", "profile-update"}
        and feishu_delivery is None
    ):
        raise ValueError(f"Feishu delivery configuration is required in --{mode} mode")
    if mode in {"send", "dry-run", *profile_modes} and model_config is None:
        raise ValueError(f"model configuration is required in --{mode} mode")
    if issue_date is not None:
        if mode not in {"send", "dry-run"}:
            raise ValueError("--issue-date is only valid with --send or --dry-run")
        if date.fromisoformat(issue_date).isoformat() != issue_date:
            raise ValueError("--issue-date must use YYYY-MM-DD")
    if scheduled and (mode != "send" or issue_date is not None):
        raise ValueError("--scheduled requires --send and cannot use --issue-date")
    started_at = _now_beijing()
    if scheduled and not (570 <= started_at.hour * 60 + started_at.minute <= 750):
        print(
            f"Scheduled send skipped: outside 09:30–12:30 Beijing window ({started_at.isoformat()}).",
            file=output,
        )
        return 0
    if mode == "profile-update" and started_at.hour < 19:
        print(
            "Profile update skipped: before today's 19:00 Beijing cutoff.", file=output
        )
        return 0
    if (
        mode in {"send", "profile-rebuild", "profile-update"}
        and feishu_delivery.receive_id_type != "chat_id"
    ):
        raise ValueError("personalized delivery requires a Feishu group chat_id")

    lock_context = (
        sender_lock(database_path)
        if mode in {"send", "calibrate", "profile-rebuild", "profile-update"}
        else nullcontext()
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
                issue_date=issue_date,
                scheduled=scheduled,
                started_at=started_at,
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
    issue_date: str | None,
    scheduled: bool,
    started_at: datetime,
) -> int:
    storage = SQLiteStorage(database_path)
    stats = RunStats()
    run_date = beijing_date(started_at)
    read_only = mode in {"dry-run", "profile-rebuild-preview"}
    if not read_only:
        storage.initialize()
    if scheduled:
        return await _run_scheduled(
            config,
            storage=storage,
            output=output,
            run_date=run_date,
            feishu_delivery=feishu_delivery,
            model_config=model_config,
        )

    dated_item = (
        _select_dated_issue(config.source, config, storage, issue_date, output)
        if issue_date is not None
        else None
    )

    notifier = None
    if feishu_delivery is not None:
        notifier = FeishuNotifier(feishu_delivery, config.network.timeout_seconds)

    llm = None
    profile = PreferenceProfile.empty()
    snapshot = ProfileSnapshot()
    if model_config is not None:
        llm = PersonalizationLLM(model_config, resolve_api_key(model_config))
        try:
            profile, snapshot = await resolve_profile(
                storage,
                llm,
                notifier,
                config,
                mode=mode,
                started_at=started_at,
                output=output,
            )
            if profile is None:
                await llm.close()
                return 0
        except Exception:
            await llm.close()
            raise

    try:
        if mode in {"profile-update", "profile-rebuild", "profile-rebuild-preview"}:
            payload = profile.as_display_dict()
            if mode == "profile-rebuild-preview":
                payload["entry_evidence"] = {
                    entry.entry_id: list(entry.evidence_ids)
                    for entry in profile.entries
                }
            print(
                json.dumps(payload, ensure_ascii=False, indent=2),
                file=output,
            )
            return 0

        await _process_source(
            config,
            storage,
            mode=mode,
            read_only=read_only,
            run_date=run_date,
            issue_date=issue_date,
            dated_item=dated_item,
            notifier=notifier,
            llm=llm,
            profile=profile,
            snapshot=snapshot,
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


async def _run_scheduled(
    config: AppConfig,
    *,
    storage: SQLiteStorage,
    output: IO[str],
    run_date: str,
    feishu_delivery: FeishuDeliveryConfig,
    model_config: ModelConfig,
) -> int:
    stats = RunStats()
    state = IssueState(storage)
    pending = []
    # Finish all durable collection before constructing or calling the model.
    source = config.source
    saved = _saved_issues(storage, source)
    today = saved.get(run_date)
    if today is not None:
        print(
            f"RSS skipped: {source.name}: complete snapshot for {run_date}; "
            f"first_saved_at={state.first_saved_at(today[0].dedupe_key)}",
            file=output,
            flush=True,
        )
    else:
        print(
            f"RSS fetch started: {source.name}: {_now_beijing().isoformat()}",
            file=output,
            flush=True,
        )
        batch, succeeded = _collect(source, config, stats, output)
        if succeeded:
            items = _unique_items(batch.items, stats)
            valid_items = []
            for item in items:
                try:
                    day = issue_date_for(item)
                    print(
                        f"RSS issue: date={day} published_at={item.published_at} key={item.dedupe_key}",
                        file=output,
                        flush=True,
                    )
                    valid_items.append(item)
                    fresh, _ = _partition_by_age(
                        [item], run_date=run_date, max_age_days=source.max_age_days
                    )
                    if not fresh:
                        stats.skipped += 1
                        continue
                    if day in saved:
                        continue
                    _, articles = prepare_issue(item, selected_date=day)
                    save_issue(storage, item, day, articles)
                    saved[day] = (item, articles)
                    print(
                        f"Complete issue saved: {day}: articles={len(articles)} first_saved_at={state.first_saved_at(item.dedupe_key)}",
                        file=output,
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001 - never mark a failed parse/save fetched
                    stats.failed += 1
                    print(
                        f"Issue parse/save failed: {item.title}: {exc}",
                        file=output,
                        flush=True,
                    )
            if (
                not storage.is_source_initialized(source.name)
                and not batch.issues
                and valid_items
            ):
                storage.initialize_source_baseline(source.name, valid_items)
                stats.baseline += len(valid_items)
                print(
                    f"Baseline created: {source.name}: {len(valid_items)} issue(s)",
                    file=output,
                )
    for day, (item, articles) in saved.items():
        fresh, _ = _partition_by_age(
            [item], run_date=run_date, max_age_days=source.max_age_days
        )
        if not fresh or not storage.unseen([item]):
            stats.skipped += 1
            continue
        pending.append((day, source, item, articles))
    pending.sort(key=lambda entry: (entry[0], entry[1].name))
    if not pending:
        print(
            "No pending issues; preference, evaluation and Feishu stages skipped.",
            file=output,
        )
        _print_summary(stats, output)
        return 1 if stats.failed else 0
    profile = storage.active_profile(read_only=True)
    if profile is None or profile.version <= 0:
        print(
            "No saved preference profile; complete issue snapshots retained, sending deferred to after 19:00 update.",
            file=output,
        )
        stats.failed += 1
        _print_summary(stats, output)
        return 1
    feedback = storage.feedback_evidence(
        read_only=True, cutoff=profile.last_feedback_revision_id
    )
    print(
        f"Using saved preference v{profile.version}; feedback_cutoff={profile.last_feedback_revision_id}; pending_dates={','.join(entry[0] for entry in pending)}",
        file=output,
        flush=True,
    )
    llm = PersonalizationLLM(model_config, resolve_api_key(model_config))
    try:
        notifier = FeishuNotifier(feishu_delivery, config.network.timeout_seconds)
        for day, source, item, articles in pending:
            print(f"Processing saved issue: {day}", file=output, flush=True)
            await process_issue(
                source,
                item,
                mode="send",
                storage=storage,
                notifier=notifier,
                llm=llm,
                profile=profile,
                feedback=feedback,
                config=config,
                output=output,
                stats=stats,
                saved_articles=articles,
                selected_date=day,
            )
            if storage.unseen([item]):
                print(
                    f"Issue incomplete: {day}; later dates deferred to preserve order.",
                    file=output,
                    flush=True,
                )
                break
    finally:
        await llm.close()
    _print_summary(stats, output)
    return 1 if stats.failed else 0


def _select_dated_issue(
    source: SourceConfig,
    config: AppConfig,
    storage: SQLiteStorage,
    issue_date: str,
    output: IO[str],
) -> tuple[NewsItem, tuple[DigestArticle, ...] | None]:
    try:
        batch = collect_source(source, config.network)
    except Exception as exc:  # noqa: BLE001 - permit full-snapshot fallback
        print(
            f"RSS unavailable ({type(exc).__name__}); looking for full issue snapshot.",
            file=output,
        )
    else:
        matches = {
            item.dedupe_key: item
            for item in batch.items
            if issue_date_for(item) == issue_date
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
    items: Sequence[NewsItem], *, run_date: str, max_age_days: int | None
) -> tuple[list[NewsItem], list[NewsItem]]:
    if max_age_days is None:
        return list(items), []
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


def _collect(source, config, stats, output):
    try:
        batch = collect_source(source, config.network)
    except Exception as exc:  # noqa: BLE001 - permit recovery from saved issues
        stats.failed += 1
        print(
            f"Collection failed: {source.name}: {type(exc).__name__}; continuing saved issues.",
            file=output,
            flush=True,
        )
        return CollectionBatch(()), False
    stats.failed += len(batch.issues)
    print(
        f"Collection completed: {source.name}: returned={len(batch.items)} warnings={len(batch.issues)}",
        file=output,
        flush=True,
    )
    return batch, True


def _saved_issues(storage, source):
    return {
        day: (item, articles)
        for day, item, articles in IssueState(storage).saved_issues(source.name)
    }


async def _process_source(
    config,
    storage,
    *,
    mode,
    read_only,
    run_date,
    issue_date,
    dated_item,
    notifier,
    llm,
    profile,
    snapshot,
    output,
    stats,
):
    source = config.source
    if issue_date is not None:
        item, saved_articles = dated_item
        await process_issue(
            source,
            item,
            mode=mode,
            storage=storage,
            notifier=notifier,
            llm=llm,
            profile=profile,
            feedback=snapshot.feedback,
            config=config,
            output=output,
            stats=stats,
            saved_articles=saved_articles,
            selected_date=issue_date,
        )
        return
    batch, collection_succeeded = _collect(source, config, stats, output)
    saved = (
        {
            item.dedupe_key: (day, item, articles)
            for day, (item, articles) in _saved_issues(storage, source).items()
        }
        if mode != "calibrate"
        else {}
    )
    items = _unique_items(
        (*batch.items, *(entry[1] for entry in saved.values())), stats
    )
    items.sort(key=issue_date_for, reverse=True)
    if mode == "calibrate":
        if not items:
            stats.failed += 1
            print(f"Calibration found no issue: {source.name}", file=output)
            return
        if not storage.is_source_initialized(source.name):
            storage.initialize_source_baseline(source.name, items)
        # Calibration intentionally targets only the newest issue.
        # Seal the older RSS window so the following personalized
        # send cannot replay historical daily issues.  A later
        # explicit calibration can still ignore this whole-issue
        # state and send its own snapshot cards.
        storage.record_delivered(items[1:])
        await process_issue(
            source,
            items[0],
            mode=mode,
            storage=storage,
            notifier=notifier,
            llm=None,
            profile=profile,
            feedback=snapshot.feedback,
            config=config,
            output=output,
            stats=stats,
        )
        return

    initialized = storage.is_source_initialized(source.name, read_only=read_only)
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
        elif collection_succeeded and not batch.issues:
            storage.initialize_source_baseline(source.name, items)
        return

    try:
        unseen = storage.unseen(items, read_only=read_only)
    except Exception as exc:  # noqa: BLE001 - isolate source state
        stats.failed += 1
        print(
            f"State lookup failed: {source.name}: {type(exc).__name__}",
            file=output,
        )
        return
    stats.skipped += len(items) - len(unseen)
    selected = sorted(unseen, key=issue_date_for)
    if source.max_age_days is not None:
        selected, stale = _partition_by_age(
            selected,
            run_date=run_date,
            max_age_days=source.max_age_days,
        )
        stats.skipped += len(stale)
        if stale:
            print(
                f"Skipped {len(stale)} issue(s) older than {source.max_age_days} days.",
                file=output,
            )
    for item in selected:
        await process_issue(
            source,
            item,
            mode=mode,
            storage=storage,
            notifier=notifier,
            llm=llm,
            profile=profile,
            feedback=snapshot.feedback,
            config=config,
            output=output,
            stats=stats,
        )
