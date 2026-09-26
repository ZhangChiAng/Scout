"""Manage Scout's isolated ChatGPT login through the official Codex SDK."""

from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import suppress
from pathlib import Path

from openai_codex import __version__ as SDK_VERSION

from .codex_runtime import (
    INTERRUPT_TIMEOUT_SECONDS,
    AuthenticationRequiredError,
    CodexSession,
    LLMError,
    model_error,
    require_chatgpt,
)
from .config import ConfigError, load_dotenv

LOGIN_TIMEOUT_SECONDS = 15 * 60
STATUS_TIMEOUT_SECONDS = 60


def _show_status(session: CodexSession, response) -> None:
    account = require_chatgpt(response)
    metadata = session.metadata
    version = (
        metadata.serverInfo.version
        if metadata.serverInfo is not None
        else metadata.userAgent
    )
    print(f"Authenticated with ChatGPT; plan={account.plan_type.value}.")
    print(f"Codex SDK={SDK_VERSION}; runtime={version}.")
    print(f"Scout Codex state: {session.home}")


async def _login(session: CodexSession, *, browser: bool) -> None:
    client = session.client
    login_id = None
    completed = False
    try:
        result = await client.account_login_start(
            {"type": "chatgpt" if browser else "chatgptDeviceCode"}
        )
        handle = result.root
        login_id = handle.login_id
        if browser:
            print(f"Open this URL in your browser:\n{handle.auth_url}", flush=True)
        else:
            print(
                f"Open {handle.verification_url}\n"
                f"Enter this one-time code: {handle.user_code}\n"
                "If device login is disabled, enable it in ChatGPT security settings "
                "or use: python -m scout.auth login --browser",
                flush=True,
            )
        notification = await client.wait_for_login_completed(login_id)
        if not notification.success:
            raise AuthenticationRequiredError(
                "ChatGPT login did not complete. Check device login settings or retry with --browser."
            )
        _show_status(session, await client.account_read({"refreshToken": False}))
        completed = True
    finally:
        if login_id is not None:
            if not completed:
                with suppress(Exception):
                    async with asyncio.timeout(INTERRUPT_TIMEOUT_SECONDS):
                        await client.account_login_cancel(login_id)
            client.unregister_login_notifications(login_id)


async def _run(args: argparse.Namespace) -> None:
    timeout = (
        LOGIN_TIMEOUT_SECONDS if args.command == "login" else STATUS_TIMEOUT_SECONDS
    )
    try:
        async with asyncio.timeout(timeout):
            async with CodexSession() as session:
                if args.command == "login":
                    await _login(session, browser=args.browser)
                elif args.command == "status":
                    _show_status(
                        session,
                        await session.client.account_read(
                            {"refreshToken": args.refresh}
                        ),
                    )
                else:
                    await session.client.account_logout()
                    print("Scout ChatGPT session logged out.")
    except TimeoutError as exc:
        raise LLMError(
            f"Codex {args.command} exceeded {timeout} seconds; runtime closed."
        ) from exc
    except Exception as exc:
        error = model_error(exc)
        if error is exc:
            raise
        raise error from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage Scout's ChatGPT subscription login"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser(
        "login", help="sign in with ChatGPT (device code by default)"
    )
    login.add_argument(
        "--browser", action="store_true", help="use browser callback login"
    )
    status = commands.add_parser(
        "status", help="show authentication status without a model call"
    )
    status.add_argument(
        "--refresh", action="store_true", help="ask Codex to refresh the session"
    )
    commands.add_parser(
        "logout", help="log out of Scout's dedicated Codex state directory"
    )
    args = parser.parse_args(argv)
    try:
        load_dotenv(Path.cwd() / ".env")
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Login or status operation cancelled; runtime closed.", file=sys.stderr)
        return 130
    except (ConfigError, LLMError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
