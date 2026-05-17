"""SQLite-backed storage for each user's routes.

One table (`routes`, see `schema.py`) holds every route a user owns, keyed
on `(user_id, id)` and tagged with where it came from: `web` (added through
the web UI), `garmin` (written by a Garmin sync) or `import` (imported from
a TOML config). Rows are returned as `RouteConfig` instances so the GeoJSON
builders consume them unchanged.

`sport` and `tags` are stored as JSON arrays to round-trip
`RouteConfig.sport` / `RouteConfig.tags` faithfully. `gpx` is a path
relative to the directory the caller resolves route files against.
"""

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from wishmap import db
from wishmap.models import RouteConfig

Source = Literal["web", "garmin", "import"]

# Fields a PATCH may change. `id`, `gpx` and `strava_id` are identity /
# geometry, owned by whoever created the route.
EDITABLE_FIELDS = frozenset({"name", "sport", "status", "tags", "notes"})

# Matches the numeric route id in a Strava route URL path, e.g.
# /routes/3499498993111706860 (with or without a trailing slash).
_ROUTE_ID_RE = re.compile(r"/routes/(\d+)")

_COLUMNS = "id, name, sport, status, tags, notes, gpx, strava_id"

# Upsert keyed on (user_id, id). On conflict every field is replaced —
# `source` too, so the latest writer owns the row — except `created_at`,
# which keeps the route's original creation time.
_UPSERT = """
INSERT INTO routes
    (user_id, id, name, sport, status, tags, notes, gpx, strava_id,
     source, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id, id) DO UPDATE SET
    name = excluded.name,
    sport = excluded.sport,
    status = excluded.status,
    tags = excluded.tags,
    notes = excluded.notes,
    gpx = excluded.gpx,
    strava_id = excluded.strava_id,
    source = excluded.source
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_strava_route_id(url: str) -> str | None:
    """Extract the numeric route id from a Strava route URL, else None.

    Returns a **digits-only** string (so it is always safe to embed in a
    filename — no path traversal) or None when the URL is not a
    recognizable strava.com route link. Examples:

        https://www.strava.com/routes/12345      -> "12345"
        https://www.strava.com/routes/12345/      -> "12345"
        https://www.strava.com/routes/12345?x=1   -> "12345"
        https://example.com/routes/12345          -> None  (not strava)
        https://www.strava.com/athletes/9          -> None  (no route id)
    """
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    host = (parsed.netloc or "").lower()
    if not (host == "strava.com" or host.endswith(".strava.com")):
        return None
    match = _ROUTE_ID_RE.search(parsed.path or "")
    if match is None:
        return None
    return match.group(1)


def _row_to_route(row: sqlite3.Row) -> RouteConfig:
    """Reconstruct a RouteConfig from a DB row (JSON-decoding list fields)."""
    return RouteConfig(
        id=row["id"],
        name=row["name"],
        sport=json.loads(row["sport"]),
        status=row["status"],
        tags=json.loads(row["tags"]),
        notes=row["notes"],
        gpx=row["gpx"],
        strava_id=row["strava_id"],
    )


def _params(
    user_id: int, route: RouteConfig, source: Source, created_at: str
) -> tuple[Any, ...]:
    """Bind values for `_UPSERT`, in column order."""
    return (
        user_id,
        route.id,
        route.name,
        json.dumps(route.sport),
        route.status.value,
        json.dumps(route.tags),
        route.notes,
        route.gpx,
        route.strava_id,
        source,
        created_at,
    )


def _get(conn: sqlite3.Connection, user_id: int, route_id: str) -> RouteConfig | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM routes WHERE user_id = ? AND id = ?",
        (user_id, route_id),
    ).fetchone()
    return None if row is None else _row_to_route(row)


def get_all(
    db_path: Path, user_id: int, source: Source | None = None
) -> list[RouteConfig]:
    """Return the user's routes (optionally only one `source`), oldest first.

    Pure DB read — does NOT validate that each route's GPX file still
    exists/parses. Callers that render geometry (e.g. `userdata.build`) are
    responsible for skipping routes with an unusable GPX; callers that
    only need the id set (e.g. rating validation) use the list as-is.
    """
    sql = f"SELECT {_COLUMNS} FROM routes WHERE user_id = ?"
    params: tuple[Any, ...] = (user_id,)
    if source is not None:
        sql += " AND source = ?"
        params += (source,)
    with db.connect(db_path) as conn:
        rows = conn.execute(sql + " ORDER BY created_at, id", params).fetchall()
    return [_row_to_route(row) for row in rows]


def get(db_path: Path, user_id: int, route_id: str) -> RouteConfig | None:
    """Return one of the user's routes, or None if they own no such id."""
    with db.connect(db_path) as conn:
        return _get(conn, user_id, route_id)


def insert(
    db_path: Path,
    user_id: int,
    route: RouteConfig,
    *,
    source: Source = "web",
    created_at: str | None = None,
) -> bool:
    """Insert or replace one of the user's routes, keyed on `route.id`.

    Returns True if a new row was created, False if an existing row with
    the same id was replaced (so the caller can report "added" vs
    "updated"). On replace, the original `created_at` is preserved.
    """
    with db.connect(db_path) as conn:
        # Take the write lock up front so the existence check and the
        # upsert see the same row.
        conn.execute("BEGIN IMMEDIATE")
        existed = (
            conn.execute(
                "SELECT 1 FROM routes WHERE user_id = ? AND id = ?",
                (user_id, route.id),
            ).fetchone()
            is not None
        )
        conn.execute(_UPSERT, _params(user_id, route, source, created_at or _now()))
    return not existed


def upsert_many(
    db_path: Path, user_id: int, source: Source, routes: Iterable[RouteConfig]
) -> int:
    """Insert or replace many of the user's routes in one transaction.

    `insert` for a batch (an import writes ~1000 routes at once): every
    route is tagged `source`, an existing row with the same id is replaced
    but keeps its `created_at`. Unlike `replace_source`, routes not in the
    batch are left alone. Returns how many rows were newly created.
    """
    now = _now()
    with db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM routes WHERE user_id = ?", (user_id,)
            )
        }
        batch = list(routes)
        conn.executemany(
            _UPSERT, [_params(user_id, route, source, now) for route in batch]
        )
    return len({route.id for route in batch} - existing)


def update(
    db_path: Path, user_id: int, route_id: str, patch: Mapping[str, Any]
) -> RouteConfig | None:
    """Apply a partial update to one of the user's routes.

    `patch` maps a subset of `EDITABLE_FIELDS` to new values (pass a
    request model's `model_dump(exclude_unset=True)`); fields absent from
    it are preserved. The merged route is re-validated as a `RouteConfig`,
    so a bad value raises `pydantic.ValidationError` and nothing is
    written. Returns the updated route, or None if the user owns no route
    with this id. Raises ValueError for a field outside `EDITABLE_FIELDS`.
    """
    unknown = set(patch) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    with db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _get(conn, user_id, route_id)
        if existing is None:
            return None
        route = RouteConfig.model_validate({**existing.model_dump(), **patch})
        conn.execute(
            """
            UPDATE routes
            SET name = ?, sport = ?, status = ?, tags = ?, notes = ?
            WHERE user_id = ? AND id = ?
            """,
            (
                route.name,
                json.dumps(route.sport),
                route.status.value,
                json.dumps(route.tags),
                route.notes,
                user_id,
                route_id,
            ),
        )
    return route


def delete(db_path: Path, user_id: int, route_id: str) -> bool:
    """Delete one of the user's routes. Returns True if a row was removed."""
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM routes WHERE user_id = ? AND id = ?", (user_id, route_id)
        )
        return cur.rowcount > 0


def gpx_in_use(db_path: Path, user_id: int, gpx: str) -> bool:
    """True if any of the user's routes still points at GPX file `gpx`.

    Routes can share a file (an import maps every route naming the same
    GPX onto one copy), so a delete must not remove a file still in use.
    """
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM routes WHERE user_id = ? AND gpx = ? LIMIT 1",
            (user_id, gpx),
        ).fetchone()
        return row is not None


def replace_source(
    db_path: Path, user_id: int, source: Source, routes: Iterable[RouteConfig]
) -> None:
    """Atomically make `routes` the user's complete set of `source` routes.

    Used by a sync that regenerates its whole route list each run (Garmin):
    rows of this source that are not in `routes` are deleted, the rest are
    upserted. A route that survives keeps its original `created_at` (so
    ordering is stable across syncs). Routes of other sources are left
    alone — except that an id already owned by another source is taken
    over (the latest writer wins), rather than failing the whole sync.
    Ratings are keyed on route ids, so they survive a route dropping out
    and coming back.
    """
    now = _now()
    with db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        created = {
            row["id"]: row["created_at"]
            for row in conn.execute(
                "SELECT id, created_at FROM routes WHERE user_id = ? AND source = ?",
                (user_id, source),
            )
        }
        conn.execute(
            "DELETE FROM routes WHERE user_id = ? AND source = ?", (user_id, source)
        )
        conn.executemany(
            _UPSERT,
            [
                _params(user_id, route, source, created.get(route.id, now))
                for route in routes
            ],
        )
