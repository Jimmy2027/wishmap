"""SQLite-backed storage for which users share their routes on this server.

A user who opts in (a row in `shared_maps`, see `schema.py`) makes their
routes available to every other account on the server, which may overlay
them on its own map. There is no per-user allowlist: the flag is all or
nothing. What a viewer gets is decided by the endpoint (routes only, no
notes, Strava ids, ratings or pins). Deleting the user cascades to the row.
"""

from datetime import datetime, timezone
from pathlib import Path

from wishmap import db


def is_shared(db_path: Path, user_id: int) -> bool:
    """Return whether the user shares their routes with other accounts."""
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM shared_maps WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def set_shared(db_path: Path, user_id: int, enabled: bool) -> None:
    """Opt the user in or out. Idempotent; the user must exist to opt in."""
    with db.connect(db_path) as conn:
        if enabled:
            conn.execute(
                """
                INSERT INTO shared_maps (user_id, created_at) VALUES (?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (user_id, datetime.now(timezone.utc).isoformat()),
            )
        else:
            conn.execute("DELETE FROM shared_maps WHERE user_id = ?", (user_id,))


def list_shared(db_path: Path, exclude_user_id: int) -> list[tuple[int, str]]:
    """Return `(id, username)` of every sharing user but the viewer, by name."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT users.id, users.username
            FROM shared_maps JOIN users ON users.id = shared_maps.user_id
            WHERE users.id != ?
            ORDER BY users.username COLLATE NOCASE
            """,
            (exclude_user_id,),
        ).fetchall()
    return [(row["id"], row["username"]) for row in rows]
