"""Strava activity metadata: archive import, and matching against Garmin.

Strava's API is subscriber-only, so activities come from the account's
data-export archive (`import_archive`) into a per-user SQLite DB
(`<user dir>/strava/activities.db`). A Garmin sync then looks each of its
activities up there (`find_match`) to take over the Strava name and sport.
"""

import csv
import io
import json
import logging
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any

from wishmap.exceptions import SyncError

logger = logging.getLogger("wishmap.strava")

# activities.csv is a few MB even for thousands of activities; refuse to
# inflate anything much bigger (e.g. a zip bomb posing as one).
MAX_CSV_BYTES = 64 * 1024 * 1024

# Strava sport_type -> wishmap_sport. Mirrors garmin.SPORT_MAP. Color is no
# longer stored per route — the frontend derives it from the sport (see
# SPORT_COLORS in static/index.html), so this only normalizes sport types.
STRAVA_SPORT_MAP: dict[str, str] = {
    "Hike": "hiking",
    "Run": "running",
    "TrailRun": "trail_running",
    "VirtualRun": "running",
    "Ride": "biking",
    "VirtualRide": "biking",
    "MountainBikeRide": "mountain_biking",
    "EMountainBikeRide": "mountain_biking",
    "GravelRide": "gravel",
    "EBikeRide": "biking",
    "Walk": "walking",
    "BackcountrySki": "ski_touring",
    "AlpineSki": "skiing",
    "NordicSki": "cross_country_skiing",
    "Snowshoe": "snowshoeing",
    "Snowboard": "skiing",
    "Swim": "swimming",
    "StandUpPaddling": "sup",
    "RockClimbing": "climbing",
    "Kayaking": "kayaking",
    "Canoeing": "kayaking",
    "Rowing": "rowing",
}

@dataclass
class StravaMatch:
    strava_id: int
    name: str
    sport: str


def _map_sport(sport_type: str) -> str:
    """Return the wishmap_sport for a Strava sport_type.

    Unmapped types are lowercased and passed through (the frontend hashes
    unknown sport names into a stable color)."""
    return STRAVA_SPORT_MAP.get(sport_type, sport_type.lower())


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sport_type TEXT NOT NULL,
            type TEXT NOT NULL,
            start_date TEXT NOT NULL,
            start_date_local TEXT NOT NULL,
            distance REAL NOT NULL,
            moving_time INTEGER NOT NULL,
            elapsed_time INTEGER NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_start_local ON activities(start_date_local);
        CREATE INDEX IF NOT EXISTS idx_start ON activities(start_date);
        """
    )


_STRAVA_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_garmin_gmt(s: str) -> datetime | None:
    """Parse Garmin's startTimeGMT ('YYYY-MM-DD HH:MM:SS', naive UTC)."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _parse_strava_time(s: str) -> datetime | None:
    """Parse Strava's start_date ('YYYY-MM-DDTHH:MM:SSZ', UTC)."""
    try:
        return datetime.strptime(s, _STRAVA_TIME_FMT)
    except ValueError:
        return None


def find_match(
    db_path: Path,
    garmin_start_gmt: str,
    garmin_distance_m: float,
    time_tol_sec: int = 300,
    dist_tol_pct: float = 0.05,
) -> StravaMatch | None:
    """Look up a Strava activity matching the given Garmin activity.

    Compares UTC start times (Garmin startTimeGMT vs Strava start_date):
    archive imports carry no timezone, so local times aren't available for
    every row. Returns the closest-by-time candidate within ±time_tol_sec
    whose distance is within ±dist_tol_pct of garmin_distance_m. Returns
    None if db_path is missing or no candidate matches.
    """
    if not db_path.is_file():
        return None
    g_dt = _parse_garmin_gmt(garmin_start_gmt)
    if g_dt is None:
        return None

    lo = (g_dt - timedelta(seconds=time_tol_sec)).strftime(_STRAVA_TIME_FMT)
    hi = (g_dt + timedelta(seconds=time_tol_sec)).strftime(_STRAVA_TIME_FMT)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, sport_type, start_date, distance "
            "FROM activities WHERE start_date BETWEEN ? AND ?",
            (lo, hi),
        ).fetchall()
    finally:
        conn.close()

    best: tuple[int, int, str, str] | None = None  # (time_delta_sec, id, name, sport_type)
    for row in rows:
        sid, name, sport_type, start, distance = row
        s_dt = _parse_strava_time(start)
        if s_dt is None:
            continue
        if garmin_distance_m > 0 and distance > 0:
            if abs(distance - garmin_distance_m) / garmin_distance_m > dist_tol_pct:
                continue
        delta = abs(int((s_dt - g_dt).total_seconds()))
        if best is None or delta < best[0]:
            best = (delta, sid, name, sport_type)

    if best is None:
        return None
    _, sid, name, sport_type = best
    sport = _map_sport(sport_type)
    return StravaMatch(strava_id=sid, name=name, sport=sport)


# ── Archive import ───────────────────────────────────────────────────
# Strava's "Download your data" archive (free for every account) contains
# activities.csv. Its "Activity Date" is UTC, rendered in the account's
# locale; "Activity Type" is a display name ("Backcountry Ski") rather than
# the API's sport_type ("BackcountrySki").

_ARCHIVE_DATE_FORMATS = (
    "%b %d, %Y, %I:%M:%S %p",  # English: "Dec 20, 2022, 7:08:27 AM"
    "%b %d, %Y, %H:%M:%S",
    "%d %b %Y, %H:%M:%S",
    "%d.%m.%Y, %H:%M:%S",  # German
    "%d/%m/%Y, %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)

# Display names whose API sport_type isn't just the name minus spaces/dashes.
_ARCHIVE_TYPE_ALIASES: dict[str, str] = {
    "RockClimb": "RockClimbing",
}


def _parse_archive_date(s: str) -> datetime | None:
    # Newer exports put a narrow no-break space before AM/PM.
    s = " ".join(s.replace("\u202f", " ").split())
    for fmt in _ARCHIVE_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _archive_sport_type(display: str) -> str:
    key = display.replace(" ", "").replace("-", "")
    return _ARCHIVE_TYPE_ALIASES.get(key, key)


def _read_archive_csv(archive: Path | IO[bytes]) -> str:
    """Return activities.csv text from an archive .zip or a bare .csv.

    `archive` is a path or a seekable binary file (an upload).
    """
    if zipfile.is_zipfile(archive):
        if not isinstance(archive, Path):
            archive.seek(0)
        with zipfile.ZipFile(archive) as zf:
            infos = [i for i in zf.infolist() if i.filename.endswith("activities.csv")]
            if not infos:
                raise SyncError("The archive has no activities.csv")
            # The top-level file is the shortest path.
            info = min(infos, key=lambda i: len(i.filename))
            if info.file_size > MAX_CSV_BYTES:
                raise SyncError("activities.csv in the archive is too large")
            raw = zf.read(info)
    elif isinstance(archive, Path):
        if archive.stat().st_size > MAX_CSV_BYTES:
            raise SyncError(f"{archive} is too large for an activities.csv")
        raw = archive.read_bytes()
    else:
        archive.seek(0)
        raw = archive.read(MAX_CSV_BYTES + 1)
        if len(raw) > MAX_CSV_BYTES:
            raise SyncError("The upload is too large for an activities.csv")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SyncError("activities.csv is not UTF-8 text") from None


def _float_or_zero(s: str) -> float:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return 0.0


def _parse_archive_rows(text: str) -> list[dict[str, Any]]:
    """Turn activities.csv into API-shaped activity dicts.

    The CSV repeats some headers: the first "Distance" is in the athlete's
    display unit (km or mi), the last is in meters. Columns are therefore
    looked up by position, taking the last occurrence of each name.
    """
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        raise SyncError(f"activities.csv is not valid CSV: {e}") from e
    if not rows:
        return []
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}  # last occurrence wins
    for required in ("Activity ID", "Activity Date", "Activity Name", "Activity Type", "Distance"):
        if required not in col:
            raise SyncError(f"activities.csv has no '{required}' column")

    def cell(row: list[str], name: str) -> str:
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else ""

    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        if not row:
            continue
        raw_date = cell(row, "Activity Date")
        start = _parse_archive_date(raw_date)
        if start is None:
            raise SyncError(
                f"Unrecognized 'Activity Date' format in activities.csv: {raw_date!r}"
            )
        # Activities without an uploaded file are manual entries (no GPS),
        # which no Garmin activity can match.
        if "Filename" in col and not cell(row, "Filename"):
            continue
        sport_type = _archive_sport_type(cell(row, "Activity Type"))
        raw_id = cell(row, "Activity ID")
        try:
            activity_id = int(raw_id)
        except ValueError:
            raise SyncError(
                f"Invalid 'Activity ID' in activities.csv: {raw_id!r}"
            ) from None
        out.append(
            {
                "id": activity_id,
                "name": cell(row, "Activity Name"),
                "sport_type": sport_type,
                "type": sport_type,
                "start_date": start.strftime(_STRAVA_TIME_FMT),
                # The archive has no timezone, so local time is unknown.
                # Matching uses start_date; this column is informational.
                "start_date_local": start.strftime(_STRAVA_TIME_FMT),
                "distance": _float_or_zero(cell(row, "Distance")),
                "moving_time": int(_float_or_zero(cell(row, "Moving Time"))),
                "elapsed_time": int(_float_or_zero(cell(row, "Elapsed Time"))),
                "source": "archive",
            }
        )
    return out


def import_archive(db_file: Path, archive: Path | IO[bytes]) -> int:
    """Import a Strava data-export archive into the activities DB `db_file`.

    `archive` is the export .zip or its bare activities.csv (a path or a
    seekable binary file). The DB (and its directory) is created if needed.
    Rows already present (e.g. from an older API sync) keep their other
    data; only name and sport type are refreshed from the archive. Returns
    the number of rows inserted or updated. Raises SyncError for an
    unreadable or malformed archive, before the DB is touched.
    """
    activities = _parse_archive_rows(_read_archive_csv(archive))

    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        _init_db(conn)
        for a in activities:
            conn.execute(
                """
                INSERT INTO activities
                (id, name, sport_type, type, start_date, start_date_local,
                 distance, moving_time, elapsed_time, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    sport_type = excluded.sport_type
                """,
                (
                    a["id"],
                    a["name"],
                    a["sport_type"],
                    a["type"],
                    a["start_date"],
                    a["start_date_local"],
                    a["distance"],
                    a["moving_time"],
                    a["elapsed_time"],
                    json.dumps(a),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    logger.info("Imported %d Strava archive activities into %s", len(activities), db_file)
    return len(activities)


def count_activities(db_file: Path) -> int:
    """How many activities the DB `db_file` holds (0 if it doesn't exist)."""
    if not db_file.is_file():
        return 0
    conn = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM activities").fetchone()
    except sqlite3.OperationalError:  # no activities table yet
        return 0
    finally:
        conn.close()
    return int(count)


_ACTIVITY_COLUMNS = (
    "id, name, sport_type, type, start_date, start_date_local, "
    "distance, moving_time, elapsed_time, raw_json"
)


def copy_activities(src_db: Path, dst_db: Path) -> int:
    """Merge every activity of the DB `src_db` into `dst_db` (created if missing).

    Used to move a single-user install's activities DB into an account.
    `src_db` is only read (opened read-only, so it is never migrated or
    locked for writing). A row whose id `dst_db` already holds is replaced
    by the source's copy; other `dst_db` rows are kept, so merging twice is
    harmless. Returns the number of activities copied.
    """
    src = sqlite3.connect(f"{src_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = src.execute(f"SELECT {_ACTIVITY_COLUMNS} FROM activities").fetchall()
    finally:
        src.close()

    dst_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dst_db)
    try:
        _init_db(conn)
        conn.executemany(
            f"INSERT OR REPLACE INTO activities ({_ACTIVITY_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)
