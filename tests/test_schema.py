"""Tests for the shared schema (`schema.py`), `db.connect`, and the legacy migration.

The legacy fixtures recreate the exact single-user tables older wishmap
versions wrote (`route_ratings` keyed on route_id alone, `web_routes`,
optionally with its even older `color` column), then check that opening
the DB moves every row under the unclaimed user and drops the old tables.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wishmap import db, ratings, routes_store, schema
from wishmap.app import app
from wishmap.models import Status
from wishmap.schema import UNCLAIMED_USER_ID

TABLES = {
    "users",
    "sessions",
    "pins",
    "routes",
    "track_cache",
    "route_ratings",
    "integrations",
    "public_links",
    "shared_maps",
    "api_tokens",
}

LEGACY_RATINGS_DDL = """
CREATE TABLE route_ratings (
    route_id TEXT PRIMARY KEY,
    fun INTEGER,
    difficulty INTEGER,
    scenery INTEGER,
    updated_at TEXT NOT NULL
);
"""

LEGACY_WEB_ROUTES_DDL = """
CREATE TABLE web_routes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    sport       TEXT NOT NULL,
    status      TEXT NOT NULL,
    tags        TEXT NOT NULL,
    notes       TEXT NOT NULL,
    gpx         TEXT NOT NULL,
    strava_id   INTEGER,
    created_at  TEXT NOT NULL
);
"""

# Before colors became sport-derived, web_routes also had a NOT NULL color.
LEGACY_WEB_ROUTES_COLOR_DDL = LEGACY_WEB_ROUTES_DDL.replace(
    "notes       TEXT NOT NULL,", "notes       TEXT NOT NULL,\n    color TEXT NOT NULL,"
)

GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>t</name><trkseg>
    <trkpt lat="46.9" lon="9.7"/>
    <trkpt lat="46.91" lon="9.71"/>
  </trkseg></trk>
</gpx>
"""


def _raw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(db_path: Path) -> set[str]:
    conn = _raw(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def _seed_legacy(
    db_path: Path,
    *,
    with_ratings: bool = True,
    with_web_routes: bool = True,
    color: bool = False,
) -> None:
    conn = _raw(db_path)
    try:
        if with_ratings:
            conn.executescript(LEGACY_RATINGS_DDL)
            conn.executemany(
                "INSERT INTO route_ratings VALUES (?, ?, ?, ?, ?)",
                [
                    ("garmin-1", None, 5, None, "2026-05-15T20:57:39+00:00"),
                    ("web-old", 3, 2, 4, "2026-06-01T00:00:00+00:00"),
                ],
            )
        if with_web_routes:
            conn.executescript(
                LEGACY_WEB_ROUTES_COLOR_DDL if color else LEGACY_WEB_ROUTES_DDL
            )
            cols = "id, name, sport, status, tags, notes, gpx, strava_id, created_at"
            values = (
                "web-old",
                "Old",
                '["biking"]',
                "planned",
                '["loop"]',
                "https://www.strava.com/routes/1",
                "data/strava/routes/web-old.gpx",
                42,
                "2020-01-01T00:00:00+00:00",
            )
            if color:
                cols += ", color"
                values += ("#2E86DE",)
            conn.execute(
                f"INSERT INTO web_routes ({cols}) VALUES ({', '.join('?' * len(values))})",
                values,
            )
        conn.commit()
    finally:
        conn.close()


def _migrate(db_path: Path) -> None:
    with db.connect(db_path):
        pass


# ── Fresh DB ─────────────────────────────────────────────────────────


def test_connect_creates_full_schema(db_path: Path) -> None:
    _migrate(db_path)
    assert _tables(db_path) == TABLES
    conn = _raw(db_path)
    try:
        cols = {
            table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for table in TABLES
        }
    finally:
        conn.close()
    assert cols["routes"] == {
        "user_id", "id", "name", "sport", "status", "tags", "notes",
        "gpx", "strava_id", "source", "created_at",
    }
    assert cols["route_ratings"] == {
        "user_id", "route_id", "fun", "difficulty", "scenery", "updated_at",
    }
    assert cols["pins"] == {
        "user_id", "id", "name", "lat", "lon", "sport", "status", "tags",
        "notes", "created_at",
    }


def test_ddl_is_idempotent(db_path: Path) -> None:
    _migrate(db_path)
    _migrate(db_path)
    conn = _raw(db_path)
    try:
        conn.executescript(schema.DDL)
    finally:
        conn.close()
    assert _tables(db_path) == TABLES


def test_connect_commits_on_success(db_path: Path) -> None:
    with db.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO route_ratings VALUES (1, 'r1', 4, 3, 5, 'now')"
        )
    assert ratings.get_all(db_path, 1)["r1"]["fun"] == 4


def test_connect_rolls_back_on_exception(db_path: Path) -> None:
    ratings.upsert(db_path, 1, "r1", {"fun": 1})

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with db.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO route_ratings VALUES (1, 'r2', 2, 2, 2, 'pending')"
            )
            raise Boom

    assert set(ratings.get_all(db_path, 1)) == {"r1"}


# ── Constraints ──────────────────────────────────────────────────────


def _add_user(conn: sqlite3.Connection, username: str = "alice") -> int:
    cur = conn.execute(
        "INSERT INTO users (username, pw_hash, created_at) VALUES (?, 'h', 'now')",
        (username,),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_foreign_keys_are_enforced(db_path: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO sessions VALUES ('h', 999, 'now', 'now', 'later')"
            )


def test_deleting_user_cascades_sessions_and_public_link(db_path: Path) -> None:
    with db.connect(db_path) as conn:
        uid = _add_user(conn)
        conn.execute("INSERT INTO sessions VALUES ('h', ?, 'now', 'now', 'later')", (uid,))
        conn.execute("INSERT INTO public_links VALUES ('tok', ?, 'now')", (uid,))
    with db.connect(db_path) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM public_links").fetchone()[0] == 0


def test_user_ids_start_at_one_and_never_unclaimed(db_path: Path) -> None:
    with db.connect(db_path) as conn:
        assert _add_user(conn) == 1
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO users (id, username, pw_hash, created_at) "
                "VALUES (?, 'ghost', 'h', 'now')",
                (UNCLAIMED_USER_ID,),
            )


def test_username_unique_case_insensitive(db_path: Path) -> None:
    with db.connect(db_path) as conn:
        _add_user(conn, "Alice")
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(db_path) as conn:
            _add_user(conn, "alice")


def test_one_public_link_per_user(db_path: Path) -> None:
    with db.connect(db_path) as conn:
        uid = _add_user(conn)
        conn.execute("INSERT INTO public_links VALUES ('tok1', ?, 'now')", (uid,))
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(db_path) as conn:
            conn.execute("INSERT INTO public_links VALUES ('tok2', ?, 'now')", (uid,))


def test_integration_provider_check(db_path: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO integrations (user_id, provider, updated_at) "
                "VALUES (1, 'komoot', 'now')"
            )


# ── Legacy migration ─────────────────────────────────────────────────


@pytest.mark.parametrize("color", [False, True])
def test_migrates_legacy_tables_to_unclaimed_user(db_path: Path, color: bool) -> None:
    _seed_legacy(db_path, color=color)

    _migrate(db_path)

    assert _tables(db_path) == TABLES  # web_routes + the legacy rename are gone
    assert ratings.get_all(db_path, UNCLAIMED_USER_ID) == {
        "garmin-1": {
            "fun": None,
            "difficulty": 5,
            "scenery": None,
            "updated_at": "2026-05-15T20:57:39+00:00",
        },
        "web-old": {
            "fun": 3,
            "difficulty": 2,
            "scenery": 4,
            "updated_at": "2026-06-01T00:00:00+00:00",
        },
    }
    [route] = routes_store.get_all(db_path, UNCLAIMED_USER_ID, "web")
    assert route.id == "web-old"
    assert route.name == "Old"
    assert route.sport == ["biking"]
    assert route.status == Status.PLANNED
    assert route.tags == ["loop"]
    assert route.notes == "https://www.strava.com/routes/1"
    assert route.gpx == "data/strava/routes/web-old.gpx"  # kept verbatim
    assert route.strava_id == 42
    conn = _raw(db_path)
    try:
        created_at = conn.execute("SELECT created_at FROM routes").fetchone()[0]
    finally:
        conn.close()
    assert created_at == "2020-01-01T00:00:00+00:00"


def test_migration_is_idempotent(db_path: Path) -> None:
    _seed_legacy(db_path)
    _migrate(db_path)
    ratings.upsert(db_path, UNCLAIMED_USER_ID, "garmin-2", {"fun": 1})
    before_ratings = ratings.get_all(db_path, UNCLAIMED_USER_ID)
    before_routes = routes_store.get_all(db_path, UNCLAIMED_USER_ID)

    _migrate(db_path)
    _migrate(db_path)

    assert ratings.get_all(db_path, UNCLAIMED_USER_ID) == before_ratings
    assert routes_store.get_all(db_path, UNCLAIMED_USER_ID) == before_routes
    assert _tables(db_path) == TABLES


def test_migrates_ratings_only(db_path: Path) -> None:
    _seed_legacy(db_path, with_web_routes=False)
    _migrate(db_path)
    assert set(ratings.get_all(db_path, UNCLAIMED_USER_ID)) == {"garmin-1", "web-old"}
    assert routes_store.get_all(db_path, UNCLAIMED_USER_ID) == []


def test_migrates_web_routes_only(db_path: Path) -> None:
    _seed_legacy(db_path, with_ratings=False)
    _migrate(db_path)
    assert ratings.get_all(db_path, UNCLAIMED_USER_ID) == {}
    assert [r.id for r in routes_store.get_all(db_path, UNCLAIMED_USER_ID)] == ["web-old"]


def test_migrated_rows_are_writable_through_stores(db_path: Path) -> None:
    _seed_legacy(db_path)
    _migrate(db_path)
    out = ratings.upsert(db_path, UNCLAIMED_USER_ID, "garmin-1", {"fun": 4})
    assert out["difficulty"] == 5  # merged into the migrated row
    assert routes_store.delete(db_path, UNCLAIMED_USER_ID, "web-old") is True


def test_failed_migration_rolls_back_completely(db_path: Path) -> None:
    # A web_routes table missing a column makes the copy fail *after* the
    # ratings were already moved; the whole migration must roll back.
    _seed_legacy(db_path, with_web_routes=False)
    conn = _raw(db_path)
    try:
        conn.execute("CREATE TABLE web_routes (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    legacy_tables = _tables(db_path)

    with pytest.raises(sqlite3.OperationalError):
        _migrate(db_path)

    assert _tables(db_path) == legacy_tables == {"route_ratings", "web_routes"}
    conn = _raw(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(route_ratings)")}
        count = conn.execute("SELECT COUNT(*) FROM route_ratings").fetchone()[0]
    finally:
        conn.close()
    assert "user_id" not in cols
    assert count == 2


def test_concurrent_migration_runs_once(db_path: Path) -> None:
    # Two openers both saw the legacy probe; the one that loses the race
    # for the write lock must re-probe and do nothing, not fail.
    _seed_legacy(db_path)
    late = sqlite3.connect(db_path)
    try:
        _migrate(db_path)  # the winner
        schema._migrate_legacy(late)  # the loser, past its own probe
    finally:
        late.close()
    assert len(ratings.get_all(db_path, UNCLAIMED_USER_ID)) == 2
    assert len(routes_store.get_all(db_path, UNCLAIMED_USER_ID)) == 1


def test_legacy_web_routes_recreated_after_migration_is_folded_in(
    db_path: Path,
) -> None:
    # An older build pointed at a migrated DB re-creates web_routes; the
    # next open folds its rows in, and an id already in `routes` keeps the
    # newer row.
    _seed_legacy(db_path)
    _migrate(db_path)
    routes_store.update(db_path, UNCLAIMED_USER_ID, "web-old", {"name": "Edited"})
    _seed_legacy(db_path, with_ratings=False)
    conn = _raw(db_path)
    try:
        conn.execute(
            "INSERT INTO web_routes VALUES ('web-new', 'New', '[\"hiking\"]', "
            "'idea', '[]', '', 'n.gpx', NULL, '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    _migrate(db_path)

    routes = {r.id: r for r in routes_store.get_all(db_path, UNCLAIMED_USER_ID)}
    assert set(routes) == {"web-old", "web-new"}
    assert routes["web-old"].name == "Edited"
    assert "web_routes" not in _tables(db_path)


# ── Startup migrates a legacy install end to end ─────────────────────


@pytest.fixture
def legacy_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    (tmp_path / "route.gpx").write_text(GPX)
    (tmp_path / "wishmap.toml").write_text(
        'title = "test"\n'
        "\n"
        "[[routes]]\n"
        'id = "garmin-1"\n'
        'name = "Synced"\n'
        'sport = "biking"\n'
        'status = "done"\n'
        'gpx = "route.gpx"\n'
    )
    (tmp_path / "data" / "strava" / "routes").mkdir(parents=True)
    (tmp_path / "data" / "strava" / "routes" / "web-old.gpx").write_text(GPX)
    _seed_legacy(tmp_path / "data" / "wishmap.db")
    monkeypatch.setenv("WISHMAP_CONFIG", str(tmp_path / "wishmap.toml"))
    with TestClient(app) as c:
        yield c


def test_startup_migrates_legacy_data_and_keeps_it_unclaimed(
    legacy_client: TestClient, tmp_path: Path
) -> None:
    db_path = tmp_path / "data" / "wishmap.db"
    # Lifespan already migrated, before any request.
    assert "web_routes" not in _tables(db_path)

    # The pre-account rows wait (under the unclaimed user) for an import to
    # claim them: no account sees them, and nothing touched them.
    resp = legacy_client.post(
        "/api/auth/register", json={"username": "alice", "password": "pw-12345678"}
    )
    assert resp.status_code == 201
    assert legacy_client.get("/api/ratings").json() == {}
    assert legacy_client.get("/api/routes").json()["features"] == []
    assert legacy_client.delete("/api/routes/web-old").status_code == 404

    assert set(ratings.get_all(db_path, UNCLAIMED_USER_ID)) == {"garmin-1", "web-old"}
    assert [r.id for r in routes_store.get_all(db_path, UNCLAIMED_USER_ID)] == ["web-old"]
    assert (tmp_path / "data" / "strava" / "routes" / "web-old.gpx").exists()
