"""Shared SQLite connection helper for wishmap's app-managed state.

wishmap keeps its mutable, app-managed state (per-user routes, pins,
ratings, accounts) in a single `data/wishmap.db`. Every store module opens
that DB through `connect()` so the WAL/busy-timeout/foreign-keys/schema
contract lives in one place.

The schema (`schema.py`) is migrated and created on every open, so the DB
auto-materializes on first use — no setup command, no separate migration
step.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from wishmap import schema


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a sqlite3 connection scoped to one operation.

    WAL + busy_timeout let FastAPI's threadpool dispatch concurrent
    handlers without tripping a single-writer lock. `schema.migrate` runs
    on every open — cheap (a probe query plus `CREATE … IF NOT EXISTS`),
    and it removes the "did I forget to init/migrate?" failure mode.
    Foreign keys are off by default in SQLite, so they are switched on per
    connection.

    Commits on a clean exit; rolls back and re-raises on any exception.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        schema.migrate(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
