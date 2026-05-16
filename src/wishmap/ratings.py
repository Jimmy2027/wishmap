"""SQLite-backed storage for per-route star ratings.

Ratings are a user-state overlay alongside the config-as-code route
definitions in TOML. Schema is created on every connect via
`CREATE TABLE IF NOT EXISTS`, so the DB auto-materializes on first
write — no setup command required.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS route_ratings (
    route_id TEXT PRIMARY KEY,
    fun INTEGER,
    difficulty INTEGER,
    scenery INTEGER,
    updated_at TEXT NOT NULL
);
"""

AXES = ("fun", "difficulty", "scenery")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a sqlite3 connection scoped to one operation.

    WAL + busy_timeout let FastAPI's threadpool dispatch concurrent
    handlers without tripping a single-writer lock. DDL runs every
    open — cheap, removes the "did I forget to init?" failure mode.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(DDL)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "fun": row["fun"],
        "difficulty": row["difficulty"],
        "scenery": row["scenery"],
        "updated_at": row["updated_at"],
    }


def get_all(db_path: Path) -> dict[str, dict[str, Any]]:
    """Return {route_id: {fun, difficulty, scenery, updated_at}} for every row."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT route_id, fun, difficulty, scenery, updated_at FROM route_ratings"
        ).fetchall()
    return {row["route_id"]: _row_to_dict(row) for row in rows}


def upsert(
    db_path: Path,
    route_id: str,
    patch: dict[str, int | None],
) -> dict[str, Any]:
    """Apply a partial update, preserving axes absent from `patch`.

    Pass `patch` as the result of `RouteRatingIn.model_dump(exclude_unset=True)`:
    keys present with int → set that axis; keys present with None → clear that
    axis; keys absent → preserved. Empty patch refreshes only `updated_at`.
    Returns the post-write row.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT fun, difficulty, scenery FROM route_ratings WHERE route_id = ?",
            (route_id,),
        ).fetchone()

        merged: dict[str, int | None] = {axis: None for axis in AXES}
        if existing is not None:
            for axis in AXES:
                merged[axis] = existing[axis]
        for axis, value in patch.items():
            if axis in AXES:
                merged[axis] = value

        conn.execute(
            """
            INSERT INTO route_ratings (route_id, fun, difficulty, scenery, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(route_id) DO UPDATE SET
                fun = excluded.fun,
                difficulty = excluded.difficulty,
                scenery = excluded.scenery,
                updated_at = excluded.updated_at
            """,
            (route_id, merged["fun"], merged["difficulty"], merged["scenery"], now),
        )

    return {
        "fun": merged["fun"],
        "difficulty": merged["difficulty"],
        "scenery": merged["scenery"],
        "updated_at": now,
    }
