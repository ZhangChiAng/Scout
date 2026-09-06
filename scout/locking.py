"""Shared process lock for profile changes and article senders."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RunLockedError(RuntimeError):
    """Another sender, rebuild or rollback owns this database's write lock."""


@contextmanager
def sender_lock(database_path: str | Path) -> Iterator[None]:
    path = Path(database_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.sender.lock")
    with lock_path.open("a+b") as file:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunLockedError(
                "another Scout sender, rebuild or rollback is running"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
