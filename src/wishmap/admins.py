"""SQLite-backed storage for which users are server admins.

An admin (a row in `admins`, see `schema.py`) may do what affects the
whole server rather than their own map: for now, updating and restarting
it (see `updater`). The flag is granted and revoked only on the command
line (`wishmap admin`), never over HTTP, so no request can make itself an
admin. Deleting the user cascades to the row.
"""

from datetime import datetime, timezone
from pathlib import Path

from wishmap import db


def is_admin(db_path: Path, user_id: int) -> bool:
    """Return whether the user is a server admin."""
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def set_admin(db_path: Path, user_id: int, enabled: bool) -> None:
    """Grant or revoke admin rights. Idempotent; the user must exist to grant."""
    with db.connect(db_path) as conn:
        if enabled:
            conn.execute(
                """
                INSERT INTO admins (user_id, created_at) VALUES (?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (user_id, datetime.now(timezone.utc).isoformat()),
            )
        else:
            conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))


def list_admins(db_path: Path) -> list[str]:
    """Return the usernames of every admin, by name."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT users.username
            FROM admins JOIN users ON users.id = admins.user_id
            ORDER BY users.username COLLATE NOCASE
            """
        ).fetchall()
    return [row["username"] for row in rows]
