"""SQLite-backed storage for each user's public read-only link.

A user has at most one link (`public_links.user_id` is UNIQUE, see
`schema.py`): an unguessable token that serves their map read-only at
`/u/<token>`. Rotating replaces the token, so the old URL stops working at
once; revoking deletes it. Deleting the user cascades to their link.
"""

import secrets
from datetime import datetime, timezone
from pathlib import Path

from wishmap import db

# 32 random bytes -> 43 URL-safe characters.
TOKEN_BYTES = 32


def get_token(db_path: Path, user_id: int) -> str | None:
    """Return the user's current link token, or None if they have none."""
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT token FROM public_links WHERE user_id = ?", (user_id,)
        ).fetchone()
    return None if row is None else row["token"]


def resolve(db_path: Path, token: str) -> int | None:
    """Return the id of the user a token belongs to, or None if it is unknown."""
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id FROM public_links WHERE token = ?", (token,)
        ).fetchone()
    return None if row is None else row["user_id"]


def rotate(db_path: Path, user_id: int) -> str:
    """Create the user's link, or replace its token if they have one.

    Returns the new token. The user must exist (foreign key).
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = datetime.now(timezone.utc).isoformat()
    with db.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO public_links (token, user_id, created_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                token = excluded.token,
                created_at = excluded.created_at
            """,
            (token, user_id, now),
        )
    return token


def revoke(db_path: Path, user_id: int) -> bool:
    """Delete the user's link. Returns True if they had one."""
    with db.connect(db_path) as conn:
        cur = conn.execute("DELETE FROM public_links WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0
