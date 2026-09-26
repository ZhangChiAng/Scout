"""Evaluate and deliver a daily issue, including calibration and tail lists."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import IO

from .config import AppConfig, SourceConfig
from .digest import issue_date_for, normalize_articles, parse_issue
from .issue_state import FilteredList, IssueState, ListMember
from .llm import EVALUATION_BATCH_SIZE, PersonalizationLLM
from .model import (
    DigestArticle,
    FeedbackEvidence,
    NewsItem,
    PersonalizedEvaluation,
    PreferenceProfile,
    RunStats,
)
from .notifier import (
    FeishuNotifier,
    build_article_card,
    build_filtered_list_card,
    partition_filtered_members,
)
from .storage import SQLiteStorage


async def process_issue(
    source: SourceConfig,
    item: NewsItem,
    *,
    mode: str,
    storage: SQLiteStorage,
    notifier: FeishuNotifier | None,
    llm: PersonalizationLLM | None,
    profile: PreferenceProfile,
    feedback: Sequence[FeedbackEvidence],
    config: AppConfig,
    output: IO[str],
    stats: RunStats,
    saved_articles: tuple[DigestArticle, ...] | None = None,
    selected_date: str | None = None,
) -> None:
    state = IssueState(storage)
    try:
        issue_date, articles = prepare_issue(
            item, saved_articles=saved_articles, selected_date=selected_date
        )
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
            snapshots = save_issue(storage, item, issue_date, articles)
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
    recent_likes = tuple(f for f in reversed(feedback) if f.sentiment == "like")[:4]
    recent_dislikes = tuple(f for f in reversed(feedback) if f.sentiment == "dislike")[
        :4
    ]
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
                print(
                    f"Evaluation started: {issue_date}: positions "
                    f"{missing[0].position}-{missing[-1].position}; "
                    f"profile=v{profile.version} cached={len(batch) - len(missing)}",
                    file=output,
                    flush=True,
                )
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
                print(
                    f"Evaluation completed: {issue_date}: {len(generated)} entries",
                    file=output,
                    flush=True,
                )
            else:
                print(
                    f"Evaluation cache reused: {issue_date}: {len(batch)} entries",
                    file=output,
                    flush=True,
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
                sent = notifier.send_card(card)
                storage.record_card_delivery(
                    snapshot_id=snapshot_id,
                    article_key=article.article_key,
                    purpose="personalized",
                    message_id=sent.message_id,
                    chat_id=sent.chat_id,
                    evaluation=evaluation,
                )
                stats.sent += 1
                print(
                    f"Article sent: {issue_date} #{article.position}",
                    file=output,
                    flush=True,
                )
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
            print(f"Issue completed: {issue_date}", file=output, flush=True)
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
    notifier: FeishuNotifier | None,
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
            sent = notifier.send_card(card)
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


def prepare_issue(item, *, saved_articles=None, selected_date=None):
    if saved_articles is None:
        issue = parse_issue(item.content, page_url=item.url)
        articles = normalize_articles(issue, digest_key=item.dedupe_key)
        day = selected_date or issue.issue_date or issue_date_for(item)
    else:
        articles = saved_articles
        day = selected_date or issue_date_for(item)
    if not articles:
        raise ValueError("issue overview contains no entries")
    return day, articles


def save_issue(storage, item, day, articles):
    snapshots = storage.save_article_snapshots(articles)
    IssueState(storage).save_issue(item, day, articles)
    return snapshots
