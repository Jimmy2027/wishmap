"""Tests for Strava archive import and UTC-based Garmin↔Strava matching."""

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from wishmap import strava
from wishmap.exceptions import SyncError

# Trimmed real-world header: note the duplicated "Elapsed Time" and
# "Distance" columns (first Distance in km, last in meters).
HEADER = (
    "Activity ID,Activity Date,Activity Name,Activity Type,Activity Description,"
    "Elapsed Time,Distance,Max Heart Rate,Relative Effort,Commute,"
    "Activity Private Note,Activity Gear,Filename,Elapsed Time,Moving Time,"
    "Distance,Max Speed"
)


def _row(aid: int, date: str, name: str, typ: str, km: str, m: str, filename: str) -> str:
    return (
        f'{aid},"{date}","{name}",{typ},,3600,{km},,,false,,,{filename},'
        f"3600,3500,{m},5.0"
    )


CSV = "\n".join(
    [
        HEADER,
        _row(1, "Dec 20, 2022, 7:08:27 AM", "Morning Tour", "Backcountry Ski",
             "12.50", "12500.0", "activities/1.fit.gz"),
        _row(2, "Jun 1, 2026, 5:00:00 PM", "Evening Ride", "Mountain Bike Ride",
             "30.10", "30100.0", "activities/2.fit.gz"),
        _row(3, "Jun 2, 2026, 6:00:00 PM", "Gym", "Weight Training",
             "0.00", "0.0", ""),
    ]
)


def _db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "strava" / "activities.db"


def _zip(**files: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_parse_archive_rows() -> None:
    rows = strava._parse_archive_rows(CSV)
    # Row 3 has no uploaded file (manual entry) and is skipped.
    assert [r["id"] for r in rows] == [1, 2]
    first = rows[0]
    assert first["sport_type"] == "BackcountrySki"
    assert first["start_date"] == "2022-12-20T07:08:27Z"
    assert first["distance"] == 12500.0  # meters, from the last Distance column
    assert rows[1]["sport_type"] == "MountainBikeRide"
    assert rows[1]["start_date"] == "2026-06-01T17:00:00Z"


def test_unknown_date_format_is_reported() -> None:
    bad = HEADER + "\n" + _row(1, "sometime", "x", "Ride", "1", "1000", "a.gpx")
    with pytest.raises(SyncError, match="sometime"):
        strava._parse_archive_rows(bad)


def test_missing_column_is_reported() -> None:
    with pytest.raises(SyncError, match="Distance"):
        strava._parse_archive_rows("Activity ID,Activity Date,Activity Name,Activity Type\n")


def test_import_from_zip_and_match(tmp_path: Path) -> None:
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("activities.csv", CSV)

    assert strava.import_archive(_db(tmp_path), archive) == 2
    assert strava.count_activities(_db(tmp_path)) == 2

    # Garmin reports startTimeGMT; 2 minutes off and 1% longer still matches.
    match = strava.find_match(_db(tmp_path), "2022-12-20 07:10:27", 12625.0)
    assert match is not None
    assert match.strava_id == 1
    assert match.name == "Morning Tour"
    assert match.sport == "ski_touring"

    # Too far apart in distance → no match.
    assert strava.find_match(_db(tmp_path), "2022-12-20 07:08:27", 20000.0) is None


def test_import_keeps_api_data_for_existing_rows(tmp_path: Path) -> None:
    # A row from the API sync of the single-user days.
    db = _db(tmp_path)
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    strava._init_db(conn)
    conn.execute(
        "INSERT INTO activities VALUES (1, 'Old name', 'BackcountrySki', "
        "'BackcountrySki', '2022-12-20T07:08:27Z', '2022-12-20T08:08:27Z', "
        "12500.0, 3500, 3600, '{}')"
    )
    conn.commit()
    conn.close()

    csv_path = tmp_path / "activities.csv"
    csv_path.write_text(CSV)
    strava.import_archive(db, csv_path)

    conn = sqlite3.connect(db)
    name, local, raw = conn.execute(
        "SELECT name, start_date_local, raw_json FROM activities WHERE id = 1"
    ).fetchone()
    conn.close()
    assert name == "Morning Tour"  # refreshed from the archive
    assert local == "2022-12-20T08:08:27Z"  # API-only data kept
    assert "source" not in json.loads(raw)


@pytest.mark.parametrize(
    "upload",
    [
        _zip(**{"export_1/activities.csv": CSV, "export_1/media/activities.csv": "x"}),
        CSV.encode("utf-8-sig"),
    ],
    ids=["zip", "csv"],
)
def test_import_from_an_uploaded_file(tmp_path: Path, upload: bytes) -> None:
    # The web upload hands over a (spooled) binary file, not a path.
    assert strava.import_archive(_db(tmp_path), io.BytesIO(upload)) == 2


@pytest.mark.parametrize(
    ("upload", "match"),
    [
        (_zip(**{"other.csv": CSV}), "no activities.csv"),
        (b"\xff\xfe not utf-8 \x81", "not UTF-8"),
        (
            (
                b"Activity ID,Activity Date,Activity Name,Activity Type,Distance\n"
                b'abc,"Dec 20, 2022, 7:08:27 AM",x,Ride,1\n'
            ),
            "Activity ID",
        ),
    ],
    ids=["zip-without-csv", "binary", "bad-id"],
)
def test_bad_uploads_are_rejected_before_the_db(
    tmp_path: Path, upload: bytes, match: str
) -> None:
    with pytest.raises(SyncError, match=match):
        strava.import_archive(_db(tmp_path), io.BytesIO(upload))
    assert not _db(tmp_path).exists()


def test_oversized_csv_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(strava, "MAX_CSV_BYTES", 100)
    with pytest.raises(SyncError, match="too large"):
        strava.import_archive(_db(tmp_path), io.BytesIO(CSV.encode()))
    with pytest.raises(SyncError, match="too large"):
        strava.import_archive(_db(tmp_path), io.BytesIO(_zip(**{"activities.csv": CSV})))


def test_count_activities_without_a_db(tmp_path: Path) -> None:
    assert strava.count_activities(tmp_path / "missing.db") == 0


def test_copy_activities_merges_into_a_new_or_existing_db(tmp_path: Path) -> None:
    archive = tmp_path / "activities.csv"
    archive.write_text(CSV)
    src = _db(tmp_path)
    strava.import_archive(src, archive)

    dst = tmp_path / "user" / "strava" / "activities.db"
    assert strava.copy_activities(src, dst) == 2
    match = strava.find_match(dst, "2022-12-20 07:10:27", 12625.0)
    assert match is not None and match.strava_id == 1

    # A row only the destination has survives; merging again is harmless.
    conn = sqlite3.connect(dst)
    conn.execute(
        "INSERT INTO activities VALUES (99, 'Own', 'Run', 'Run', "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1000.0, 1, 1, '{}')"
    )
    conn.commit()
    conn.close()
    assert strava.copy_activities(src, dst) == 2
    conn = sqlite3.connect(dst)
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM activities ORDER BY id")]
    finally:
        conn.close()
    assert ids == [1, 2, 99]
