"""SQLite-backed storage for each user's pins.

The pin counterpart of `routes_store`: one `pins` table (see `schema.py`)
keyed on `(user_id, id)`, rows returned as `PinConfig` so the GeoJSON
builders consume them unchanged. `sport` and `tags` are JSON arrays.
"""

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wishmap import db
from wishmap.models import PinConfig

# Fields a PATCH may change: everything but the id.
EDITABLE_FIELDS = frozenset({"name", "lat", "lon", "sport", "status", "tags", "notes"})

_COLUMNS = "id, name, lat, lon, sport, status, tags, notes"


def _row_to_pin(row: sqlite3.Row) -> PinConfig:
    """Reconstruct a PinConfig from a DB row (JSON-decoding list fields)."""
    return PinConfig(
        id=row["id"],
        name=row["name"],
        lat=row["lat"],
        lon=row["lon"],
        sport=json.loads(row["sport"]),
        status=row["status"],
        tags=json.loads(row["tags"]),
        notes=row["notes"],
    )


def _get(conn: sqlite3.Connection, user_id: int, pin_id: str) -> PinConfig | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM pins WHERE user_id = ? AND id = ?",
        (user_id, pin_id),
    ).fetchone()
    return None if row is None else _row_to_pin(row)


def get_all(db_path: Path, user_id: int) -> list[PinConfig]:
    """Return every pin the user owns, oldest first."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM pins WHERE user_id = ? ORDER BY created_at, id",
            (user_id,),
        ).fetchall()
    return [_row_to_pin(row) for row in rows]


def get(db_path: Path, user_id: int, pin_id: str) -> PinConfig | None:
    """Return one of the user's pins, or None if they own no such id."""
    with db.connect(db_path) as conn:
        return _get(conn, user_id, pin_id)


def insert(
    db_path: Path, user_id: int, pin: PinConfig, created_at: str | None = None
) -> bool:
    """Insert or replace one of the user's pins, keyed on `pin.id`.

    Returns True if a new row was created, False if an existing pin was
    replaced. On replace, the original `created_at` is preserved.
    """
    now = created_at or datetime.now(timezone.utc).isoformat()
    with db.connect(db_path) as conn:
        # Take the write lock up front so the existence check and the
        # upsert see the same row.
        conn.execute("BEGIN IMMEDIATE")
        existed = _get(conn, user_id, pin.id) is not None
        conn.execute(
            """
            INSERT INTO pins
                (user_id, id, name, lat, lon, sport, status, tags, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, id) DO UPDATE SET
                name = excluded.name,
                lat = excluded.lat,
                lon = excluded.lon,
                sport = excluded.sport,
                status = excluded.status,
                tags = excluded.tags,
                notes = excluded.notes
            """,
            (
                user_id,
                pin.id,
                pin.name,
                pin.lat,
                pin.lon,
                json.dumps(pin.sport),
                pin.status.value,
                json.dumps(pin.tags),
                pin.notes,
                now,
            ),
        )
    return not existed


def update(
    db_path: Path, user_id: int, pin_id: str, patch: Mapping[str, Any]
) -> PinConfig | None:
    """Apply a partial update to one of the user's pins.

    Same contract as `routes_store.update`: `patch` holds a subset of
    `EDITABLE_FIELDS` (absent fields are preserved), the merged pin is
    re-validated (`pydantic.ValidationError` on a bad value, nothing
    written), and the result is None if the user owns no such pin.
    Raises ValueError for a field outside `EDITABLE_FIELDS`.
    """
    unknown = set(patch) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    with db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _get(conn, user_id, pin_id)
        if existing is None:
            return None
        pin = PinConfig.model_validate({**existing.model_dump(), **patch})
        conn.execute(
            """
            UPDATE pins
            SET name = ?, lat = ?, lon = ?, sport = ?, status = ?, tags = ?, notes = ?
            WHERE user_id = ? AND id = ?
            """,
            (
                pin.name,
                pin.lat,
                pin.lon,
                json.dumps(pin.sport),
                pin.status.value,
                json.dumps(pin.tags),
                pin.notes,
                user_id,
                pin_id,
            ),
        )
    return pin


def delete(db_path: Path, user_id: int, pin_id: str) -> bool:
    """Delete one of the user's pins. Returns True if a row was removed."""
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM pins WHERE user_id = ? AND id = ?", (user_id, pin_id)
        )
        return cur.rowcount > 0
