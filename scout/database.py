"""SQLite connections and short transactions shared by Scout state stores."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


def connect(path, *, read_only=False, rows=False, timeout=5):
    target = Path(path).resolve().as_uri() + "?mode=ro" if read_only else path
    conn = sqlite3.connect(target, uri=read_only, timeout=timeout)
    conn.execute("PRAGMA foreign_keys = ON")
    if rows:
        conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(path, *, rows=False, timeout=5):
    with closing(connect(path, rows=rows, timeout=timeout)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            yield conn
