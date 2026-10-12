"""The `data/wishmap.db` schema and its idempotent migration.

Every table wishmap keeps in SQLite is defined here, in one place, so the
store modules (`routes_store`, `pins_store`, `ratings`, …) only hold
queries. `db.connect` calls `migrate()` on every open: it upgrades a
legacy single-user DB in place (once), then runs the `CREATE … IF NOT
EXISTS` script, so a fresh or old DB is always usable with no setup step.

Every per-user table is keyed on `user_id`. Rows written before accounts
existed belong to `UNCLAIMED_USER_ID` (0) — no real account ever gets that
id (see the CHECK on `users.id`) — until an import claims them.

Legacy layout (single-user, before accounts), migrated by `migrate()`:
  - `route_ratings(route_id PK, …)` -> `route_ratings` keyed on
    `(user_id, route_id)`, every row given `user_id = 0`.
  - `web_routes(id PK, …)` -> copied into `routes` with `user_id = 0,
    source = 'web'`, then dropped. Its GPX paths are kept verbatim
    (relative to the config directory, as before).

This module is a leaf (stdlib only), so `db` can depend on it.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

# The owner of rows written before multi-user support ("unclaimed").
UNCLAIMED_USER_ID = 0

# `sport` / `tags` columns hold JSON arrays and `settings` a JSON object;
# timestamps are ISO-8601 UTC strings. Kept as separate statements (not one
# script) so `_migrate_legacy` can run them inside its own transaction —
# `executescript` would commit it.
_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY CHECK (id > 0),
        username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
        pw_hash       TEXT NOT NULL,
        -- The vault key wrapped under a password-derived key; NULL until
        -- the account has a vault.
        vault_salt    BLOB,
        vault_wrapped BLOB,
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        -- sha256 of the session cookie; the token itself is never stored.
        token_hash   TEXT PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at   TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id)",
    """
    CREATE TABLE IF NOT EXISTS pins (
        user_id    INTEGER NOT NULL,
        id         TEXT NOT NULL,
        name       TEXT NOT NULL,
        lat        REAL NOT NULL,
        lon        REAL NOT NULL,
        sport      TEXT NOT NULL,
        status     TEXT NOT NULL,
        tags       TEXT NOT NULL,
        notes      TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routes (
        user_id    INTEGER NOT NULL,
        id         TEXT NOT NULL,
        name       TEXT NOT NULL,
        sport      TEXT NOT NULL,
        status     TEXT NOT NULL,
        tags       TEXT NOT NULL,
        notes      TEXT NOT NULL,
        gpx        TEXT NOT NULL,
        strava_id  INTEGER,
        source     TEXT NOT NULL CHECK (source IN ('web', 'garmin', 'import')),
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS track_cache (
        -- Display geometry of one GPX file (see `track_cache.py`), keyed
        -- on the route's `gpx` path; a pure cache, safe to empty.
        user_id  INTEGER NOT NULL,
        gpx      TEXT NOT NULL,
        mtime_ns INTEGER NOT NULL,
        size     INTEGER NOT NULL,
        version  INTEGER NOT NULL,
        parts    TEXT NOT NULL,
        PRIMARY KEY (user_id, gpx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS route_ratings (
        user_id    INTEGER NOT NULL,
        route_id   TEXT NOT NULL,
        fun        INTEGER,
        difficulty INTEGER,
        scenery    INTEGER,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, route_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS integrations (
        user_id    INTEGER NOT NULL,
        provider   TEXT NOT NULL CHECK (provider IN ('garmin', 'strava')),
        username   TEXT,
        settings   TEXT NOT NULL DEFAULT '{}',
        -- Provider tokens sealed with the user's vault key; never plaintext.
        tokens_enc BLOB,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, provider)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS public_links (
        token      TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_tokens (
        -- Personal API tokens for scripts (see `api_tokens.py`); like
        -- sessions, only the sha256 of the token is stored.
        id           INTEGER PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        scope        TEXT NOT NULL CHECK (scope IN ('read', 'write')),
        created_at   TEXT NOT NULL,
        last_used_at TEXT,
        -- NULL = never expires.
        expires_at   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS api_tokens_user_id ON api_tokens(user_id)",
    """
    CREATE TABLE IF NOT EXISTS admins (
        -- Presence = the user may administer the server (self-update);
        -- granted only on the command line (`wishmap admin`).
        user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shared_maps (
        -- Presence = the user shares their routes with every other account.
        user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL
    )
    """,
)

DDL = "".join(f"{stmt.strip()};\n" for stmt in _STATEMENTS)

# One cheap query (no table scan) telling which legacy tables still exist:
# `web_routes` at all, and a `route_ratings` that predates `user_id`.
_LEGACY_PROBE = """
SELECT
    EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'web_routes'),
    EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'route_ratings')
        AND NOT EXISTS (
            SELECT 1 FROM pragma_table_info('route_ratings') WHERE name = 'user_id'
        )
"""


def _legacy_tables(conn: sqlite3.Connection) -> tuple[bool, bool]:
    """Return (has legacy web_routes, has legacy route_ratings)."""
    web_routes, ratings = conn.execute(_LEGACY_PROBE).fetchone()
    return bool(web_routes), bool(ratings)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the DB behind `conn` up to the current schema. Idempotent.

    Runs on every open (via `db.connect`), so the common case must be
    cheap: one probe query, then the `IF NOT EXISTS` DDL. A legacy DB is
    migrated once, atomically — either every legacy row lands under user 0
    and the old tables are gone, or nothing changed.

    Must be called outside a transaction (it commits).
    """
    if any(_legacy_tables(conn)):
        _migrate_legacy(conn)
    conn.executescript(DDL)


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """Move legacy single-user tables to the per-user schema (see module doc).

    BEGIN IMMEDIATE takes the write lock before re-probing, so two
    processes opening the same legacy DB at once migrate it exactly once:
    the second waits on the lock (busy_timeout), then finds nothing to do.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        has_web_routes, has_legacy_ratings = _legacy_tables(conn)
        if has_legacy_ratings:
            # Rename out of the way so the DDL below creates the new shape.
            conn.execute("ALTER TABLE route_ratings RENAME TO legacy_route_ratings")
        for stmt in _STATEMENTS:
            conn.execute(stmt)

        ratings_moved = routes_moved = 0
        if has_legacy_ratings:
            ratings_moved = conn.execute(
                """
                INSERT INTO route_ratings
                    (user_id, route_id, fun, difficulty, scenery, updated_at)
                SELECT ?, route_id, fun, difficulty, scenery, updated_at
                FROM legacy_route_ratings
                """,
                (UNCLAIMED_USER_ID,),
            ).rowcount
            conn.execute("DROP TABLE legacy_route_ratings")
        if has_web_routes:
            # Explicit column list: very old DBs also carry a `color` column,
            # which is simply left behind. OR IGNORE: should the new table
            # already hold the id (e.g. an older build re-created web_routes
            # after a migration), the newer `routes` row wins.
            routes_moved = conn.execute(
                """
                INSERT OR IGNORE INTO routes
                    (user_id, id, name, sport, status, tags, notes, gpx,
                     strava_id, source, created_at)
                SELECT ?, id, name, sport, status, tags, notes, gpx,
                       strava_id, 'web', created_at
                FROM web_routes
                """,
                (UNCLAIMED_USER_ID,),
            ).rowcount
            conn.execute("DROP TABLE web_routes")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if has_legacy_ratings or has_web_routes:
        logger.info(
            "Migrated legacy wishmap.db to per-user tables: %d rating(s), "
            "%d web route(s) now owned by unclaimed user %d",
            ratings_moved,
            routes_moved,
            UNCLAIMED_USER_ID,
        )
