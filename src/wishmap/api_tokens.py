"""Personal API tokens (`api_tokens` table, see `schema.py`).

A token lets a script or agent act for its owner with an
`Authorization: Bearer <token>` header instead of a browser session (see
`auth.api_user`). Like a session token it is shown once, at creation; the
DB keeps only its sha256. Each token has a name, a scope — `read` (GET
the pins, routes and ratings) or `write` (also change them) — an optional
expiry and a last-used timestamp. Tokens never reach the account,
integrations or vault endpoints, which still need a session.

Revoking deletes the row; an expired token stays listed (as expired) until
its owner revokes it. Deleting the user cascades to their tokens.
"""

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from wishmap import db
from wishmap.users import User

Scope = Literal["read", "write"]

# Makes a token recognisable (in logs, secret scanners); 32 random bytes
# -> 43 URL-safe characters after it.
TOKEN_PREFIX = "wm_"
TOKEN_BYTES = 32
# Keeps one account from growing the table without bound.
MAX_TOKENS_PER_USER = 50
# `last_used_at` is refreshed at most this often, so authenticated reads
# don't each cost a DB write.
_TOUCH_INTERVAL = timedelta(minutes=5)


class TooManyTokensError(Exception):
    """The user already has `MAX_TOKENS_PER_USER` tokens."""


@dataclass(frozen=True)
class ApiToken:
    """A token as its owner sees it in the list (never the secret itself)."""

    id: int
    name: str
    scope: Scope
    created_at: str
    last_used_at: str | None
    expires_at: str | None


@dataclass(frozen=True)
class TokenAuth:
    """A resolved token: whose it is and what it may do."""

    user: User
    scope: Scope
    token_id: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _from_row(row: sqlite3.Row) -> ApiToken:
    return ApiToken(
        id=row["id"],
        name=row["name"],
        scope=row["scope"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        expires_at=row["expires_at"],
    )


_COLUMNS = "id, name, scope, created_at, last_used_at, expires_at"


def create(
    db_path: Path,
    user_id: int,
    name: str,
    scope: Scope,
    lifetime: timedelta | None = None,
) -> tuple[str, ApiToken]:
    """Create a token for the user; return it (the only time it's in the
    clear) with its listing entry. `lifetime` None means it never expires.

    Raises TooManyTokensError at `MAX_TOKENS_PER_USER`.
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    now = _now()
    expires_at = None if lifetime is None else _iso(now + lifetime)
    with db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM api_tokens WHERE user_id = ?", (user_id,)
        ).fetchone()
        if count >= MAX_TOKENS_PER_USER:
            raise TooManyTokensError(user_id)
        cur = conn.execute(
            """
            INSERT INTO api_tokens
                (user_id, name, token_hash, scope, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, name, _token_hash(token), scope, _iso(now), expires_at),
        )
        token_id = cur.lastrowid
        assert token_id is not None
    return token, ApiToken(
        id=token_id,
        name=name,
        scope=scope,
        created_at=_iso(now),
        last_used_at=None,
        expires_at=expires_at,
    )


def list_tokens(db_path: Path, user_id: int) -> list[ApiToken]:
    """The user's tokens, oldest first."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM api_tokens WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [_from_row(row) for row in rows]


def revoke(db_path: Path, user_id: int, token_id: int) -> bool:
    """Delete one of the user's tokens. Returns True if they had it."""
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM api_tokens WHERE user_id = ? AND id = ?", (user_id, token_id)
        )
        return cur.rowcount > 0


def resolve(db_path: Path, token: str) -> TokenAuth | None:
    """Return whose a token is and its scope; None if unknown or expired."""
    # Real tokens are 46 chars; don't hash arbitrary junk.
    if not token.startswith(TOKEN_PREFIX) or len(token) > 128:
        return None
    token_hash = _token_hash(token)
    now = _now()
    with db.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT t.id, t.scope, t.last_used_at, t.expires_at, u.id AS user_id,
                   u.username
            FROM api_tokens t JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] is not None and (
            datetime.fromisoformat(row["expires_at"]) <= now
        ):
            return None
        last_used = row["last_used_at"]
        if last_used is None or now - datetime.fromisoformat(last_used) >= _TOUCH_INTERVAL:
            conn.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                (_iso(now), row["id"]),
            )
    return TokenAuth(
        user=User(id=row["user_id"], username=row["username"]),
        scope=row["scope"],
        token_id=row["id"],
    )
