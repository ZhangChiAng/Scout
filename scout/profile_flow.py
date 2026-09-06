"""Preference generation and durable profile notifications."""

from __future__ import annotations

from dataclasses import replace
from typing import IO

from .config import AppConfig
from .llm import (
    MODEL_REASONING_EFFORT,
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
        f"reasoning_effort={MODEL_REASONING_EFFORT} "
        f"input_chars={request.input_chars} (characters, not tokens).",
        file=output,
        flush=True,
    )
    try:
        generated = await llm.summarize_preferences(request)
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
