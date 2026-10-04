"""SQLite connection setup: WAL mode, foreign keys on, explicit transactions."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with WAL journaling and foreign-key enforcement.

    isolation_level=None turns off the sqlite3 module's implicit transactions,
    so the only transactions are the ones opened by `transaction()` below.
    check_same_thread=False is for the dashboard: Streamlit may run each rerun
    on a different thread, but reruns of one session never overlap.
    """
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    # foreign_keys is per-connection, so it must be set on every connect,
    # not just once in schema.sql.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block of writes atomically: all commit, or none do.

    Not re-entrant. Nesting is refused rather than silently merged into the
    outer transaction, so a caller can't believe it committed when it didn't.
    """
    if conn.in_transaction:
        raise RuntimeError("transaction() does not nest; a transaction is already open")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
