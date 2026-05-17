"""SQLite cache of each GPX file's display geometry.

Parsing a GPX (and thinning it, see `gpx.track_parts`) is nearly all of a
map build's cost — ~50s for a 1.2k-route account — so `userdata.build`
keeps the result per file in `track_cache`, keyed on `(user_id, gpx)` and
stamped with the file's mtime, size and `gpx.TRACK_VERSION`. A stamp that
no longer matches is a miss and the file is parsed again; entries whose
GPX no route references any more are pruned by the build. The table is a
pure cache: emptying it only costs one slow build.

`parts` is the JSON of `track_parts`' output (a list of `[lon, lat]` lines).
"""

import json
from dataclasses import dataclass
from pathlib import Path

from wishmap import db
from wishmap.gpx import TRACK_VERSION


@dataclass(frozen=True)
class Stamp:
    """What a cached entry is valid for: the GPX file as it was, the code as it is."""

    mtime_ns: int
    size: int
    version: int = TRACK_VERSION

    @classmethod
    def of(cls, path: Path) -> "Stamp":
        st = path.stat()
        return cls(mtime_ns=st.st_mtime_ns, size=st.st_size)


@dataclass(frozen=True)
class Entry:
    stamp: Stamp
    parts: list[list[list[float]]]


def get_all(db_path: Path, user_id: int) -> dict[str, Entry]:
    """Return the user's cached geometry, keyed on the GPX path."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT gpx, mtime_ns, size, version, parts FROM track_cache "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {
        row["gpx"]: Entry(
            stamp=Stamp(row["mtime_ns"], row["size"], row["version"]),
            parts=json.loads(row["parts"]),
        )
        for row in rows
    }


def update(
    db_path: Path, user_id: int, fresh: dict[str, Entry], keep: set[str]
) -> None:
    """Store `fresh` entries and drop every other entry not in `keep`."""
    with db.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO track_cache
                (user_id, gpx, mtime_ns, size, version, parts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    user_id,
                    gpx,
                    e.stamp.mtime_ns,
                    e.stamp.size,
                    e.stamp.version,
                    json.dumps(e.parts, separators=(",", ":")),
                )
                for gpx, e in fresh.items()
            ],
        )
        stale = [
            (user_id, gpx)
            for (gpx,) in conn.execute(
                "SELECT gpx FROM track_cache WHERE user_id = ?", (user_id,)
            )
            if gpx not in keep
        ]
        conn.executemany(
            "DELETE FROM track_cache WHERE user_id = ? AND gpx = ?", stale
        )
