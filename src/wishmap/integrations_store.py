"""SQLite-backed storage for each user's third-party integrations.

One row per `(user_id, provider)` in the `integrations` table (see
`schema.py`): the provider account name, a JSON object of non-secret
settings (e.g. Garmin's `activity_limit` / `sport_filter`) and the
provider's tokens, sealed with the user's vault key. This module never
sees plaintext tokens — callers seal/unseal around it.
"""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from wishmap import db

Provider = Literal["garmin", "strava"]

_COLUMNS = "provider, username, settings, tokens_enc, updated_at"


@dataclass(frozen=True)
class Integration:
    provider: Provider
    username: str | None
    settings: dict[str, Any]
    # Vault-sealed provider tokens, or None when not logged in.
    tokens_enc: bytes | None
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_integration(row: sqlite3.Row) -> Integration:
    return Integration(
        provider=row["provider"],
        username=row["username"],
        settings=json.loads(row["settings"]),
        tokens_enc=row["tokens_enc"],
        updated_at=row["updated_at"],
    )


def _get(
    conn: sqlite3.Connection, user_id: int, provider: Provider
) -> Integration | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM integrations WHERE user_id = ? AND provider = ?",
        (user_id, provider),
    ).fetchone()
    return None if row is None else _row_to_integration(row)


def get(db_path: Path, user_id: int, provider: Provider) -> Integration | None:
    """Return the user's integration with `provider`, or None if unset."""
    with db.connect(db_path) as conn:
        return _get(conn, user_id, provider)


def get_all(db_path: Path, user_id: int) -> list[Integration]:
    """Return every integration the user has configured, by provider."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM integrations WHERE user_id = ? ORDER BY provider",
            (user_id,),
        ).fetchall()
    return [_row_to_integration(row) for row in rows]


def upsert_settings(
    db_path: Path,
    user_id: int,
    provider: Provider,
    *,
    username: str | None,
    settings: Mapping[str, Any],
) -> Integration:
    """Create or replace the account name and settings of an integration.

    Stored tokens are kept as they are, unless `username` changes: tokens
    belong to one provider account, so switching accounts drops them.
    Returns the post-write row.
    """
    with db.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO integrations (user_id, provider, username, settings, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                tokens_enc = CASE
                    WHEN username IS excluded.username THEN tokens_enc
                END,
                username = excluded.username,
                settings = excluded.settings,
                updated_at = excluded.updated_at
            """,
            (user_id, provider, username, json.dumps(dict(settings)), _now()),
        )
        integration = _get(conn, user_id, provider)
    assert integration is not None
    return integration


def set_tokens(
    db_path: Path,
    user_id: int,
    provider: Provider,
    tokens_enc: bytes | None,
    *,
    for_username: str | None = None,
) -> bool:
    """Store (or, with None, forget) the sealed tokens of an integration.

    Returns False — writing nothing — if the user has no such integration;
    tokens only make sense for an account configured via `upsert_settings`.
    With `for_username`, also only if the integration is still for that
    provider account: tokens a sync obtained must not land on a row whose
    account the user switched meanwhile.
    """
    sql = (
        "UPDATE integrations SET tokens_enc = ?, updated_at = ? "
        "WHERE user_id = ? AND provider = ?"
    )
    params: tuple[Any, ...] = (tokens_enc, _now(), user_id, provider)
    if for_username is not None:
        sql += " AND username = ?"
        params += (for_username,)
    with db.connect(db_path) as conn:
        return conn.execute(sql, params).rowcount > 0


def delete(db_path: Path, user_id: int, provider: Provider) -> bool:
    """Remove an integration entirely. Returns True if a row was removed."""
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM integrations WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        return cur.rowcount > 0
