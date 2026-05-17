"""SQLite-backed storage for per-user, per-route star ratings.

Ratings are keyed on `(user_id, route_id)` in the `route_ratings` table
(see `schema.py`). They reference route ids loosely (no foreign key), so a
rating survives its route dropping out of a sync and coming back.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wishmap import db

AXES = ("fun", "difficulty", "scenery")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "fun": row["fun"],
        "difficulty": row["difficulty"],
        "scenery": row["scenery"],
        "updated_at": row["updated_at"],
    }


def get_all(db_path: Path, user_id: int) -> dict[str, dict[str, Any]]:
    """Return {route_id: {fun, difficulty, scenery, updated_at}} for the user."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT route_id, fun, difficulty, scenery, updated_at "
            "FROM route_ratings WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {row["route_id"]: _row_to_dict(row) for row in rows}


def upsert(
    db_path: Path,
    user_id: int,
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
    with db.connect(db_path) as conn:
        # Take the write lock before reading, so two concurrent patches of
        # different axes can't each merge into a stale row.
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT fun, difficulty, scenery FROM route_ratings "
            "WHERE user_id = ? AND route_id = ?",
            (user_id, route_id),
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
            INSERT INTO route_ratings
                (user_id, route_id, fun, difficulty, scenery, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, route_id) DO UPDATE SET
                fun = excluded.fun,
                difficulty = excluded.difficulty,
                scenery = excluded.scenery,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                route_id,
                merged["fun"],
                merged["difficulty"],
                merged["scenery"],
                now,
            ),
        )

    return {
        "fun": merged["fun"],
        "difficulty": merged["difficulty"],
        "scenery": merged["scenery"],
        "updated_at": now,
    }
