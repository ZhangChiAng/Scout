"""Command-line entry point for ``python -m scout``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from .app import run
from .collector import CollectionError
from .config import (
    ConfigError,
    load_config,
    load_dotenv,
    resolve_feishu_delivery,
)
from .feedback import FeedbackListenerError, listen_feedback
from .llm import LLMError, load_required_model_config
from .locking import RunLockedError
from .notifier import NotificationError
from .storage import SQLiteStorage, StorageError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scout: a personal AI signal scout for Feishu"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="preview personalized cards with zero SQLite writes",
    )
    mode.add_argument(
        "--send", action="store_true", help="personalize and send unseen articles"
    )
    mode.add_argument(
        "--calibrate",
        action="store_true",
        help="send every article in the latest issue as calibration cards",
    )
    mode.add_argument(
        "--listen-feedback",
        action="store_true",
        help="run the blocking Feishu long-connection feedback listener",
    )
    mode.add_argument(
        "--profile-show", action="store_true", help="show the active preference profile"
    )
    mode.add_argument(
        "--profile-history", action="store_true", help="show preference profile history"
    )
    mode.add_argument(
        "--profile-rebuild-preview",
        action="store_true",
        help="preview a full profile rebuild with the real model and zero SQLite writes",
    )
    mode.add_argument(
        "--profile-rebuild",
        action="store_true",
        help="rebuild and activate preferences, notify Feishu, without sending news",
    )
    mode.add_argument(
        "--profile-rollback",
        metavar="VERSION",
        type=int,
        help="copy an old profile into a new active version",
    )
    parser.add_argument("--config", default="config.toml", help="TOML config path")
    parser.add_argument(
        "--issue-date",
        metavar="YYYY-MM-DD",
        help="select exactly one issue for --send/--dry-run, ignoring age and baseline",
    )
    parser.add_argument(
        "--models-config", default="models.toml", help="model TOML config path"
    )
    args = parser.parse_args(argv)
    if args.issue_date is not None:
        if not (args.send or args.dry_run):
            parser.error("--issue-date is only valid with --send or --dry-run")
        try:
            if date.fromisoformat(args.issue_date).isoformat() != args.issue_date:
                raise ValueError
        except ValueError:
            parser.error("--issue-date must be a valid YYYY-MM-DD date")

    try:
        load_dotenv(Path.cwd() / ".env")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    database_path = Path(os.environ.get("SCOUT_DB_PATH", "data/scout.sqlite3"))

    try:
        if args.profile_show:
            profile = SQLiteStorage(database_path).active_profile(read_only=True)
            if profile is None:
                print("No active preference profile.")
            else:
                print(
                    json.dumps(profile.as_display_dict(), ensure_ascii=False, indent=2)
                )
            return 0
        if args.profile_history:
            history = SQLiteStorage(database_path).profile_history(read_only=True)
            if not history:
                print("No preference profile history.")
            for entry in history:
                print(json.dumps(entry, ensure_ascii=False))
            return 0
        if args.profile_rollback is not None:
            if args.profile_rollback <= 0:
                raise ConfigError("profile rollback VERSION must be positive")
            profile = SQLiteStorage(database_path).rollback_profile(
                args.profile_rollback
            )
            print(
                f"Preference profile v{profile.version} is active after rollback "
                f"from v{args.profile_rollback}."
            )
            return 0

        config = load_config(args.config)
        if args.listen_feedback:
            listen_feedback(
                resolve_feishu_delivery(),
                database_path=database_path,
                max_payload_bytes=config.feishu.max_payload_bytes,
                timeout_seconds=config.network.timeout_seconds,
            )
            return 0

        if args.calibrate:
            return run(
                config,
                mode="calibrate",
                database_path=database_path,
                output=sys.stdout,
                feishu_delivery=resolve_feishu_delivery(),
            )

        selected_mode = "send" if args.send else "dry-run"
        if args.profile_rebuild:
            selected_mode = "profile-rebuild"
        elif args.profile_rebuild_preview:
            selected_mode = "profile-rebuild-preview"
        model_config = load_required_model_config(args.models_config)
        return run(
            config,
            mode=selected_mode,
            database_path=database_path,
            output=sys.stdout,
            feishu_delivery=(
                resolve_feishu_delivery() if args.send or args.profile_rebuild else None
            ),
            model_config=model_config,
            issue_date=args.issue_date,
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (
        CollectionError,
        ConfigError,
        FeedbackListenerError,
        LLMError,
        NotificationError,
        OSError,
        RunLockedError,
        StorageError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
