"""Command-line entry point for ``python -m scout``."""

import argparse
import logging
import os
import sys
from pathlib import Path

from .app import run
from .collector import CollectionError
from .config import (
    ConfigError,
    load_config,
    load_dotenv,
    resolve_feishu_delivery,
)
from .llm import load_optional_model_config
from .notifier import NotificationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scout: a personal AI signal scout for Feishu"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the planned daily issue without local writes",
    )
    mode.add_argument(
        "--send", action="store_true", help="send the daily issue and failure alerts"
    )
    parser.add_argument("--config", default="config.toml", help="TOML config path")
    parser.add_argument(
        "--models-config", default="models.toml", help="optional model TOML config path"
    )
    args = parser.parse_args(argv)

    selected_mode = "send" if args.send else "dry-run"
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
        config = load_config(args.config)
        load_optional_model_config(args.models_config)
        feishu_delivery = resolve_feishu_delivery() if selected_mode == "send" else None
        return run(
            config,
            mode=selected_mode,
            database_path=database_path,
            output=sys.stdout,
            feishu_delivery=feishu_delivery,
        )
    except (
        CollectionError,
        ConfigError,
        NotificationError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
