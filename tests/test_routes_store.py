"""Unit tests for the routes_store DB module and the Strava-URL parser.

Mirrors test_ratings.py: drive the module directly against a tmp_path DB
(the `db_path` fixture in conftest.py), no HTTP layer.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from wishmap import routes_store
from wishmap.models import RouteConfig, Status

USER = 1
OTHER = 2


def _route(route_id: str = "web-strava-123", **overrides: object) -> RouteConfig:
    base: dict[str, object] = {
        "id": route_id,
        "name": "Test Route",
        "sport": ["biking"],
        "status": "idea",
        "tags": ["loop", "gravel"],
        "notes": "https://www.strava.com/routes/123",
        "gpx": "data/strava/routes/web-strava-123.gpx",
        "strava_id": None,
    }
    base.update(overrides)
    return RouteConfig(**base)  # type: ignore[arg-type]


def _row(db_path: Path, user_id: int, route_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM routes WHERE user_id = ? AND id = ?", (user_id, route_id)
        ).fetchone()
    finally:
        conn.close()


# ── Strava URL parser ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.strava.com/routes/3499498993111706860", "3499498993111706860"),
        ("https://www.strava.com/routes/3499498993111706860/", "3499498993111706860"),
        ("https://www.strava.com/routes/123?foo=bar", "123"),
        ("https://www.strava.com/routes/123#section", "123"),
        ("https://strava.com/routes/456", "456"),
        ("http://www.strava.com/routes/789", "789"),
        ("  https://www.strava.com/routes/42  ", "42"),
        ("https://example.com/routes/123", None),
        ("https://www.strava.com/athletes/999", None),
        ("https://www.strava.com/routes/", None),
        ("https://www.strava.com/routes/abc", None),
        ("not a url at all", None),
        ("", None),
        ("https://evil.com.strava.com.attacker.net/routes/1", None),
        # Scheme-less input has no netloc -> host check fails -> None (falls
        # back to a uuid id at the endpoint). A scheme is required.
        ("www.strava.com/routes/123", None),
        ("strava.com/routes/123", None),
    ],
)
def test_parse_strava_route_id(url: str, expected: str | None) -> None:
    assert routes_store.parse_strava_route_id(url) == expected


def test_parse_strava_route_id_is_digits_only() -> None:
    # The id becomes a filename — it must never carry path separators.
    rid = routes_store.parse_strava_route_id(
        "https://www.strava.com/routes/3499498993111706860"
    )
    assert rid is not None
    assert rid.isdigit()


# ── insert / get / get_all / delete ──────────────────────────────────


def test_get_all_empty(db_path: Path) -> None:
    assert routes_store.get_all(db_path, USER) == []


def test_insert_new_returns_true(db_path: Path) -> None:
    assert routes_store.insert(db_path, USER, _route()) is True


def test_insert_round_trips_route_config(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route())
    routes = routes_store.get_all(db_path, USER)
    assert len(routes) == 1
    r = routes[0]
    assert isinstance(r, RouteConfig)
    assert r == _route()
    assert r.sport == ["biking"]               # JSON array round-trip
    assert r.tags == ["loop", "gravel"]        # JSON array round-trip
    assert r.status == Status.IDEA             # string -> enum coercion
    assert routes_store.get(db_path, USER, "web-strava-123") == r


def test_insert_defaults_to_web_source(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route())
    assert _row(db_path, USER, "web-strava-123")["source"] == "web"


def test_insert_same_id_replaces_and_returns_false(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route(name="First"))
    created = routes_store.insert(db_path, USER, _route(name="Second", status="done"))
    assert created is False
    routes = routes_store.get_all(db_path, USER)
    assert len(routes) == 1
    assert routes[0].name == "Second"
    assert routes[0].status == Status.DONE


def test_insert_preserves_created_at_on_replace(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route(), created_at="2020-01-01T00:00:00+00:00")
    routes_store.insert(
        db_path, USER, _route(name="Updated"), created_at="2099-12-31T00:00:00+00:00"
    )
    assert _row(db_path, USER, "web-strava-123")["created_at"] == "2020-01-01T00:00:00+00:00"


def test_get_all_orders_by_created_at(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route("web-b"), created_at="2022-01-01T00:00:00+00:00")
    routes_store.insert(db_path, USER, _route("web-a"), created_at="2021-01-01T00:00:00+00:00")
    assert [r.id for r in routes_store.get_all(db_path, USER)] == ["web-a", "web-b"]


def test_get_all_filters_by_source(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route("web-1"))
    routes_store.insert(db_path, USER, _route("garmin-1"), source="garmin")
    routes_store.insert(db_path, USER, _route("trip"), source="import")
    assert [r.id for r in routes_store.get_all(db_path, USER, "garmin")] == ["garmin-1"]
    assert [r.id for r in routes_store.get_all(db_path, USER, "web")] == ["web-1"]
    assert len(routes_store.get_all(db_path, USER)) == 3


def test_get_missing_returns_none(db_path: Path) -> None:
    assert routes_store.get(db_path, USER, "nope") is None


def test_delete_existing_returns_true(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route())
    assert routes_store.delete(db_path, USER, "web-strava-123") is True
    assert routes_store.get_all(db_path, USER) == []


def test_delete_missing_returns_false(db_path: Path) -> None:
    assert routes_store.delete(db_path, USER, "nope") is False


def test_gpx_in_use_sees_other_routes_of_the_user_only(db_path: Path) -> None:
    gpx = "gpx/import/shared.gpx"
    routes_store.insert(db_path, USER, _route("a", gpx=gpx))
    routes_store.insert(db_path, USER, _route("b", gpx=gpx))
    routes_store.insert(db_path, OTHER, _route("c", gpx=gpx))
    routes_store.delete(db_path, USER, "a")
    assert routes_store.gpx_in_use(db_path, USER, gpx) is True
    routes_store.delete(db_path, USER, "b")
    # Another user's row with the same relative path is their own file.
    assert routes_store.gpx_in_use(db_path, USER, gpx) is False


def test_multiple_routes_coexist(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route("web-1", name="One"))
    routes_store.insert(db_path, USER, _route("web-2", name="Two"))
    assert {r.id for r in routes_store.get_all(db_path, USER)} == {"web-1", "web-2"}
    routes_store.delete(db_path, USER, "web-1")
    assert {r.id for r in routes_store.get_all(db_path, USER)} == {"web-2"}


# ── Per-user scoping ─────────────────────────────────────────────────


def test_same_id_is_independent_per_user(db_path: Path) -> None:
    # Two users may own a route with the same id (e.g. the same Strava
    # route): separate rows, and neither insert reports a "replace".
    assert routes_store.insert(db_path, USER, _route(name="Mine")) is True
    assert routes_store.insert(db_path, OTHER, _route(name="Theirs")) is True
    assert routes_store.get_all(db_path, USER)[0].name == "Mine"
    assert routes_store.get_all(db_path, OTHER)[0].name == "Theirs"


def test_user_cannot_read_update_or_delete_others_routes(db_path: Path) -> None:
    routes_store.insert(db_path, OTHER, _route())
    assert routes_store.get_all(db_path, USER) == []
    assert routes_store.get(db_path, USER, "web-strava-123") is None
    assert routes_store.update(db_path, USER, "web-strava-123", {"name": "X"}) is None
    assert routes_store.delete(db_path, USER, "web-strava-123") is False
    # Untouched for its owner.
    assert routes_store.get(db_path, OTHER, "web-strava-123") == _route()


# ── update (PATCH) ───────────────────────────────────────────────────


def test_update_changes_only_patched_fields(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route(), created_at="2020-01-01T00:00:00+00:00")
    out = routes_store.update(
        db_path, USER, "web-strava-123", {"name": "Renamed", "status": "done"}
    )
    assert out is not None
    assert out.name == "Renamed"
    assert out.status == Status.DONE
    assert out.tags == ["loop", "gravel"]  # preserved
    assert routes_store.get(db_path, USER, "web-strava-123") == out
    row = _row(db_path, USER, "web-strava-123")
    assert row["created_at"] == "2020-01-01T00:00:00+00:00"
    assert row["source"] == "web"


def test_update_normalizes_sport_string(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route())
    out = routes_store.update(
        db_path, USER, "web-strava-123", {"sport": "hiking", "tags": []}
    )
    assert out is not None
    assert out.sport == ["hiking"]
    assert routes_store.get(db_path, USER, "web-strava-123") == out


def test_update_missing_returns_none(db_path: Path) -> None:
    assert routes_store.update(db_path, USER, "nope", {"name": "X"}) is None


def test_update_invalid_value_raises_and_writes_nothing(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route())
    with pytest.raises(ValidationError):
        routes_store.update(
            db_path, USER, "web-strava-123", {"name": "X", "status": "bogus"}
        )
    assert routes_store.get(db_path, USER, "web-strava-123") == _route()


@pytest.mark.parametrize("field", ["id", "gpx", "strava_id", "source"])
def test_update_rejects_non_editable_fields(db_path: Path, field: str) -> None:
    routes_store.insert(db_path, USER, _route())
    with pytest.raises(ValueError, match=field):
        routes_store.update(db_path, USER, "web-strava-123", {field: "x"})
    assert routes_store.get(db_path, USER, "web-strava-123") == _route()


# ── replace_source (Garmin sync) ─────────────────────────────────────


def test_replace_source_inserts_updates_and_drops(db_path: Path) -> None:
    routes_store.replace_source(
        db_path, USER, "garmin", [_route("garmin-1"), _route("garmin-2")]
    )
    assert {r.id for r in routes_store.get_all(db_path, USER, "garmin")} == {
        "garmin-1",
        "garmin-2",
    }

    routes_store.replace_source(
        db_path,
        USER,
        "garmin",
        [_route("garmin-2", name="Renamed"), _route("garmin-3")],
    )
    routes = {r.id: r for r in routes_store.get_all(db_path, USER, "garmin")}
    assert set(routes) == {"garmin-2", "garmin-3"}  # garmin-1 dropped out
    assert routes["garmin-2"].name == "Renamed"


def test_replace_source_preserves_created_at_of_survivors(db_path: Path) -> None:
    routes_store.insert(
        db_path, USER, _route("garmin-1"), source="garmin",
        created_at="2020-01-01T00:00:00+00:00",
    )
    routes_store.replace_source(
        db_path, USER, "garmin", [_route("garmin-1"), _route("garmin-2")]
    )
    assert _row(db_path, USER, "garmin-1")["created_at"] == "2020-01-01T00:00:00+00:00"
    assert _row(db_path, USER, "garmin-2")["created_at"] > "2020"


def test_replace_source_leaves_other_sources_and_users_alone(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route("web-1"))
    routes_store.insert(db_path, USER, _route("trip"), source="import")
    routes_store.insert(db_path, OTHER, _route("garmin-9"), source="garmin")

    routes_store.replace_source(db_path, USER, "garmin", [])

    assert {r.id for r in routes_store.get_all(db_path, USER)} == {"web-1", "trip"}
    assert [r.id for r in routes_store.get_all(db_path, OTHER)] == ["garmin-9"]


def test_replace_source_takes_over_id_from_other_source(db_path: Path) -> None:
    # An id already owned by another source must not fail the whole sync.
    routes_store.insert(db_path, USER, _route("garmin-1", name="Imported"), source="import")
    routes_store.replace_source(db_path, USER, "garmin", [_route("garmin-1", name="Synced")])
    assert [r.name for r in routes_store.get_all(db_path, USER)] == ["Synced"]
    assert _row(db_path, USER, "garmin-1")["source"] == "garmin"


def test_replace_source_is_atomic(db_path: Path) -> None:
    # A failure part-way through rolls back the delete too: the previous
    # set survives intact.
    routes_store.replace_source(db_path, USER, "garmin", [_route("garmin-1")])

    class Boom(Exception):
        pass

    def failing_routes() -> Iterator[RouteConfig]:
        yield _route("garmin-2")
        raise Boom

    with pytest.raises(Boom):
        routes_store.replace_source(db_path, USER, "garmin", failing_routes())
    assert [r.id for r in routes_store.get_all(db_path, USER)] == ["garmin-1"]


# ── upsert_many (import) ─────────────────────────────────────────────


def test_upsert_many_inserts_and_counts_new_rows(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route("trip-1"), source="import")
    created = routes_store.upsert_many(
        db_path, USER, "import", [_route("trip-1", name="Renamed"), _route("trip-2")]
    )
    assert created == 1
    routes = {r.id: r for r in routes_store.get_all(db_path, USER, "import")}
    assert set(routes) == {"trip-1", "trip-2"}
    assert routes["trip-1"].name == "Renamed"


def test_upsert_many_keeps_unlisted_routes_and_created_at(db_path: Path) -> None:
    routes_store.insert(
        db_path, USER, _route("trip-1"), source="import",
        created_at="2020-01-01T00:00:00+00:00",
    )
    routes_store.insert(db_path, USER, _route("web-1"))
    routes_store.insert(db_path, OTHER, _route("trip-1"), source="import")

    assert routes_store.upsert_many(db_path, USER, "import", [_route("trip-1")]) == 0

    assert {r.id for r in routes_store.get_all(db_path, USER)} == {"trip-1", "web-1"}
    assert _row(db_path, USER, "trip-1")["created_at"] == "2020-01-01T00:00:00+00:00"
    assert len(routes_store.get_all(db_path, OTHER)) == 1


def test_upsert_many_takes_over_id_from_other_source(db_path: Path) -> None:
    routes_store.insert(db_path, USER, _route("garmin-1"), source="import")
    assert routes_store.upsert_many(db_path, USER, "garmin", [_route("garmin-1")]) == 0
    assert _row(db_path, USER, "garmin-1")["source"] == "garmin"


def test_upsert_many_is_atomic(db_path: Path) -> None:
    class Boom(Exception):
        pass

    def failing_routes() -> Iterator[RouteConfig]:
        yield _route("trip-1")
        raise Boom

    with pytest.raises(Boom):
        routes_store.upsert_many(db_path, USER, "import", failing_routes())
    assert routes_store.get_all(db_path, USER) == []


def test_source_check_constraint(db_path: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        routes_store.insert(db_path, USER, _route(), source="bogus")  # type: ignore[arg-type]
