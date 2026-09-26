"""Preference generation and durable profile notifications."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from typing import IO

from .codex_runtime import ModelUnavailableError
from .config import AppConfig
from .llm import (
    LLMError,
    PersonalizationLLM,
    preference_request,
)
from .model import PreferenceProfile, ProfileSnapshot
from .notifier import FeishuNotifier, NotificationError, build_profile_card
from .storage import SQLiteStorage


def notify_profile(
    storage: SQLiteStorage,
    notifier: FeishuNotifier,
    profile: PreferenceProfile,
    config: AppConfig,
    output: IO[str],
) -> PreferenceProfile:
    try:
        card = build_profile_card(
            profile, max_payload_bytes=config.feishu.max_payload_bytes
        )
        sent = notifier.send_card(card)
        storage.mark_profile_notified(
            profile.version, message_id=sent.message_id, chat_id=sent.chat_id
        )
    except Exception as exc:
        raise NotificationError(
            f"Profile v{profile.version} notification failed: {type(exc).__name__}; "
            "pending notification retained, article delivery is deferred."
        ) from exc
    print(
        f"Preference profile v{profile.version} notification sent.",
        file=output,
        flush=True,
    )
    return replace(profile, notified=True)


async def prepare_profile(
    storage: SQLiteStorage,
    llm: PersonalizationLLM,
    *,
    mode: str,
    output: IO[str],
    snapshot: ProfileSnapshot,
) -> PreferenceProfile:
    read_only = mode in {"dry-run", "profile-rebuild-preview"}
    request = preference_request(
        snapshot, force_rebuild=mode in {"profile-rebuild", "profile-rebuild-preview"}
    )
    if request is None:
        return snapshot.active or PreferenceProfile.empty()
    print(
        f"Preference update: mode={request.mode} trigger={request.trigger} "
        f"feedback_count={len(request.evidence)} "
        f"new_revision_count={snapshot.new_revision_count} "
        f"revisions_since_rebuild={snapshot.revisions_since_rebuild} "
        f"cutoff_revision_id={snapshot.cutoff_revision_id} "
        f"model={llm.config.model} reasoning_effort={llm.config.reasoning_effort} "
        f"input_chars={request.input_chars} (characters, not tokens).",
        file=output,
        flush=True,
    )
    try:
        generated = await llm.summarize_preferences(request)
    except ModelUnavailableError:
        raise
    except Exception as exc:
        raise LLMError(
            f"preference update failed; feedback progress unchanged: {exc}"
        ) from exc
    print(
        f"Preference result: rule_count={generated.rule_count} "
        f"entry_count={len(generated.entries)} "
        f"readable_chars={generated.as_display_dict()['readable_chars']}.",
        file=output,
        flush=True,
    )
    if read_only:
        print(
            f"Temporary preference profile v{generated.version} generated; SQLite unchanged.",
            file=output,
        )
        return generated
    saved = storage.save_profile(generated, snapshot=snapshot)
    print(f"Preference profile v{saved.version} activated.", file=output, flush=True)
    return saved


async def resolve_profile(storage, llm, notifier, config, *, mode, started_at, output):
    """Apply cutoff, resume notifications and update preferences in one place."""
    read_only = mode in {"dry-run", "profile-rebuild-preview"}
    cutoff_at = None
    if mode == "profile-update":
        cutoff_at = (
            started_at.replace(hour=19, minute=0, second=0, microsecond=0)
            .astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        print(f"Preference feedback cutoff: {cutoff_at}", file=output, flush=True)
    snapshot = storage.profile_snapshot(cutoff_at=cutoff_at)
    if (
        mode == "profile-update"
        and snapshot.active is not None
        and snapshot.active.notified
        and preference_request(snapshot) is None
    ):
        print("No preference changes or pending notification.", file=output)
        return None, snapshot
    pending = snapshot.active is not None and not snapshot.active.notified
    if pending and not read_only:
        assert notifier is not None and snapshot.active is not None
        notified = notify_profile(storage, notifier, snapshot.active, config, output)
        snapshot = replace(snapshot, active=notified)
    if mode == "profile-rebuild" and pending and not snapshot.new_revision_count:
        assert snapshot.active is not None
        profile = snapshot.active
        print(
            "Pending profile notification retried; no new model request.",
            file=output,
        )
    else:
        profile = await prepare_profile(
            storage, llm, mode=mode, output=output, snapshot=snapshot
        )
    if (
        mode in {"send", "profile-rebuild", "profile-update"}
        and profile.version > 0
        and not profile.notified
    ):
        assert notifier is not None
        profile = notify_profile(storage, notifier, profile, config, output)
    return profile, snapshot
