"""Tests for the per-user map-data cache (`userdata`) and its `UserState`.

Unit tests build an `AppContext` by hand over a tmp_path install and seed
a user's pins/routes straight into the DB (GPX under the user's dir), then
drive `userdata.build` / `userdata.ensure_loaded` directly; a few HTTP
tests check the lazy build through the app's lifespan and GET handlers.
"""

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import user_state
from wishmap import db, pins_store, routes_store, track_cache, userdata
from wishmap.geojson import pin_to_feature, route_features, route_properties
from wishmap.gpx import TRACK_VERSION, parse_gpx
from wishmap.models import PinConfig, RouteConfig, ServerSettings
from wishmap.state import AppContext, StateRegistry, UserState

GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>t</name><trkseg>
    <trkpt lat="46.9" lon="9.7"/>
    <trkpt lat="46.91" lon="9.71"/>
  </trkseg></trk>
</gpx>
"""

# A legacy single-user TOML: the server ignores its pins and routes.
LEGACY_TOML = (
    'title = "test"\n'
    "\n"
    "[[pins]]\n"
    'id = "p1"\n'
    'name = "Pin"\n'
    "lat = 46.0\n"
    "lon = 9.0\n"
    'sport = "hiking"\n'
    'status = "idea"\n'
    "\n"
    "[[routes]]\n"
    'id = "r1"\n'
    'name = "Config Route"\n'
    'sport = "hiking"\n'
    'status = "planned"\n'
    'gpx = "route.gpx"\n'
)

ALICE = 1
BOB = 2


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    (tmp_path / "data").mkdir()
    return AppContext(
        registry=StateRegistry(),
        settings=ServerSettings(title="test"),
        config_path=tmp_path / "wishmap.toml",
        base_path=tmp_path,
        data_root=tmp_path / "data",
        db_path=tmp_path / "data" / "wishmap.db",
    )


@pytest.fixture
def state(ctx: AppContext) -> UserState:
    return ctx.registry.get(ALICE)


def _ids(fc: Any) -> set[str]:
    return {f.properties.id for f in fc.features}


def _add_route(
    ctx: AppContext,
    route_id: str,
    *,
    user: int = ALICE,
    gpx_text: str = GPX,
    source: routes_store.Source = "web",
) -> RouteConfig:
    rel = f"gpx/{source}/{route_id}.gpx"
    path = ctx.user_dir(user) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gpx_text)
    route = RouteConfig(
        id=route_id,
        name="Route",
        sport=["biking"],
        status="idea",  # type: ignore[arg-type]
        gpx=rel,
    )
    routes_store.insert(ctx.db_path, user, route, source=source)
    return route


def _add_pin(ctx: AppContext, pin_id: str, *, user: int = ALICE) -> None:
    pins_store.insert(
        ctx.db_path,
        user,
        PinConfig(
            id=pin_id,
            name="Pin",
            lat=46.5,
            lon=9.5,
            sport=["hiking"],
            status="idea",  # type: ignore[arg-type]
        ),
    )


def _build(ctx: AppContext, user: int = ALICE) -> Any:
    return userdata.build(ctx.user_dir(user), ctx.db_path, user)


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    calls: list[None] = []
    real_build = userdata.build

    def counting_build(*args: Any, **kwargs: Any) -> Any:
        calls.append(None)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(userdata, "build", counting_build)
    return calls


# ── build() ──────────────────────────────────────────────────────────


def test_build_reads_the_users_pins_and_routes(ctx: AppContext) -> None:
    _add_pin(ctx, "pin-1")
    _add_route(ctx, "web-1")
    _add_route(ctx, "garmin-1", source="garmin")
    pins_fc, routes_fc = _build(ctx)
    assert _ids(routes_fc) == {"web-1", "garmin-1"}
    assert _ids(pins_fc) == {"pin-1", "web-1-start", "garmin-1-start"}


def test_build_is_scoped_to_the_user(ctx: AppContext) -> None:
    _add_pin(ctx, "alice-pin")
    _add_route(ctx, "alice-route")
    _add_pin(ctx, "bob-pin", user=BOB)
    _add_route(ctx, "bob-route", user=BOB)

    pins_fc, routes_fc = _build(ctx, ALICE)
    assert _ids(routes_fc) == {"alice-route"}
    assert _ids(pins_fc) == {"alice-pin", "alice-route-start"}
    pins_fc, routes_fc = _build(ctx, BOB)
    assert _ids(routes_fc) == {"bob-route"}
    assert _ids(pins_fc) == {"bob-pin", "bob-route-start"}


def test_build_of_an_empty_account(ctx: AppContext) -> None:
    pins_fc, routes_fc = _build(ctx)
    assert pins_fc.features == [] and routes_fc.features == []


def test_build_skips_route_with_unusable_gpx(ctx: AppContext) -> None:
    _add_route(ctx, "web-ok")
    _add_route(ctx, "web-bad", gpx_text="not xml")
    _, routes_fc = _build(ctx)
    assert _ids(routes_fc) == {"web-ok"}


def test_build_parses_each_gpx_once(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The parse is nearly all of a build's cost: the route's line and its
    # start pin must come from one parse, not one each.
    for i in range(3):
        _add_route(ctx, f"r{i}")
    parsed: list[Path] = []

    def counting_parse(path: Path) -> list[list[float]]:
        parsed.append(path)
        return parse_gpx(path)

    monkeypatch.setattr(userdata, "parse_gpx", counting_parse)
    pins_fc, routes_fc = _build(ctx)
    assert len(parsed) == 3 and len(set(parsed)) == 3
    assert _ids(routes_fc) == {"r0", "r1", "r2"}
    assert _ids(pins_fc) == {"r0-start", "r1-start", "r2-start"}


def test_build_start_pin_is_the_first_coordinate(ctx: AppContext) -> None:
    _add_route(ctx, "r1")
    pins_fc, routes_fc = _build(ctx)
    (line,) = routes_fc.features
    (start,) = pins_fc.features
    assert start.geometry.coordinates == line.geometry.coordinates[0] == [9.7, 46.9]
    assert start.properties.kind == "route_start"
    assert start.properties.model_dump(exclude={"id", "kind"}) == (
        line.properties.model_dump(exclude={"id", "kind"})
    )


# ── track_cache: parsed geometry survives a restart ──────────────────


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    parsed: list[Path] = []

    def counting_parse(path: Path) -> list[list[float]]:
        parsed.append(path)
        return parse_gpx(path)

    monkeypatch.setattr(userdata, "parse_gpx", counting_parse)
    return parsed


def test_rebuild_reads_unchanged_gpx_from_the_cache(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(3):
        _add_route(ctx, f"r{i}")
    parsed = _count_parses(monkeypatch)
    first = _build(ctx)
    assert len(parsed) == 3
    # A later build (e.g. after a server restart) parses nothing.
    assert _build(ctx) == first
    assert len(parsed) == 3


def test_changed_gpx_is_parsed_again(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = _add_route(ctx, "r1")
    _build(ctx)
    parsed = _count_parses(monkeypatch)
    moved = GPX.replace('lat="46.9" lon="9.7"', 'lat="46.905" lon="9.705"')
    (ctx.user_dir(ALICE) / route.gpx).write_text(moved)  # new size, new mtime
    pins_fc, routes_fc = _build(ctx)
    assert len(parsed) == 1
    assert routes_fc.features[0].geometry.coordinates[0] == [9.705, 46.905]
    assert pins_fc.features[0].geometry.coordinates == [9.705, 46.905]


def test_cache_built_by_older_code_is_ignored(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_route(ctx, "r1")
    _build(ctx)
    with db.connect(ctx.db_path) as conn:
        conn.execute("UPDATE track_cache SET version = version - 1")
    parsed = _count_parses(monkeypatch)
    _build(ctx)
    assert len(parsed) == 1
    (entry,) = track_cache.get_all(ctx.db_path, ALICE).values()
    assert entry.stamp.version == TRACK_VERSION


def test_cache_drops_entries_of_removed_routes(ctx: AppContext) -> None:
    _add_route(ctx, "r1")
    _add_route(ctx, "r2")
    _build(ctx)
    assert set(track_cache.get_all(ctx.db_path, ALICE)) == {
        "gpx/web/r1.gpx",
        "gpx/web/r2.gpx",
    }
    routes_store.delete(ctx.db_path, ALICE, "r2")
    _build(ctx)
    assert set(track_cache.get_all(ctx.db_path, ALICE)) == {"gpx/web/r1.gpx"}


def test_cache_is_per_user(ctx: AppContext) -> None:
    _add_route(ctx, "r1")
    _add_route(ctx, "r1", user=BOB)
    _build(ctx, ALICE)
    assert track_cache.get_all(ctx.db_path, BOB) == {}
    _build(ctx, BOB)
    assert set(track_cache.get_all(ctx.db_path, ALICE)) == {"gpx/web/r1.gpx"}


def test_failed_cache_write_does_not_fail_the_build(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_route(ctx, "r1")

    def broken_update(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(track_cache, "update", broken_update)
    _, routes_fc = _build(ctx)
    assert _ids(routes_fc) == {"r1"}


# ── ensure_loaded(): lazy build + cache ──────────────────────────────


def test_state_starts_cold(state: UserState) -> None:
    assert state.data is None


def test_first_read_builds_then_caches(
    ctx: AppContext, state: UserState, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_route(ctx, "web-1")
    calls = _count_builds(monkeypatch)
    first = userdata.ensure_loaded(ctx, state)
    second = userdata.ensure_loaded(ctx, state)
    assert len(calls) == 1
    assert second is first is state.data
    assert _ids(first.routes_fc) == {"web-1"}


def test_invalidate_rebuilds_with_new_routes(ctx: AppContext, state: UserState) -> None:
    """The sync path: new routes land in the DB, then the state is invalidated."""
    _add_route(ctx, "web-1")
    assert _ids(userdata.ensure_loaded(ctx, state).routes_fc) == {"web-1"}

    _add_route(ctx, "garmin-1", source="garmin")
    # Still the cached snapshot until invalidated.
    assert _ids(userdata.ensure_loaded(ctx, state).routes_fc) == {"web-1"}

    state.invalidate()
    assert state.data is None
    data = userdata.ensure_loaded(ctx, state)
    assert _ids(data.routes_fc) == {"web-1", "garmin-1"}


def test_build_failure_raises_and_stays_cold(
    ctx: AppContext, state: UserState, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_route(ctx, "web-1")
    real_build = userdata.build

    def broken_build(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk on fire")

    monkeypatch.setattr(userdata, "build", broken_build)
    # Any failure surfaces as UserDataError (a clean 500), never kills the worker.
    with pytest.raises(userdata.UserDataError, match="disk on fire"):
        userdata.ensure_loaded(ctx, state)
    assert state.data is None

    # Fixed: the next read retries and succeeds.
    monkeypatch.setattr(userdata, "build", real_build)
    assert _ids(userdata.ensure_loaded(ctx, state).routes_fc) == {"web-1"}


def test_concurrent_cold_reads_build_once(
    ctx: AppContext, state: UserState, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_builds(monkeypatch)
    results: list[object] = []
    start = threading.Barrier(8)

    def read() -> None:
        start.wait()
        results.append(userdata.ensure_loaded(ctx, state))

    threads = [threading.Thread(target=read) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert len(results) == 8 and all(r is results[0] for r in results)


def test_invalidate_waits_for_in_flight_build(
    ctx: AppContext, state: UserState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that read the old DB state must not land after an invalidate."""
    building = threading.Event()
    release = threading.Event()
    real_build = userdata.build

    def slow_build(*args: Any, **kwargs: Any) -> Any:
        building.set()
        assert release.wait(5)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(userdata, "build", slow_build)
    reader = threading.Thread(target=userdata.ensure_loaded, args=(ctx, state))
    reader.start()
    assert building.wait(5)

    invalidator = threading.Thread(target=state.invalidate)
    invalidator.start()
    invalidator.join(0.1)
    assert invalidator.is_alive()  # blocked on the build's data_lock

    release.set()
    reader.join(5)
    invalidator.join(5)
    assert state.data is None


# ── Swaps on UserState ───────────────────────────────────────────────


def _features(ctx: AppContext, route: RouteConfig) -> tuple[Any, Any]:
    return route_features(route, parse_gpx(ctx.user_dir(ALICE) / route.gpx))


def test_swaps_are_noops_when_cold(ctx: AppContext, state: UserState) -> None:
    route = _add_route(ctx, "web-1")
    state.swap_in_route(*_features(ctx, route))
    assert state.data is None
    state.swap_out_route("web-1")
    assert state.data is None


def test_swap_in_replaces_and_swap_out_removes(
    ctx: AppContext, state: UserState
) -> None:
    _add_pin(ctx, "pin-1")
    _add_route(ctx, "r1")
    before = userdata.ensure_loaded(ctx, state)
    route = _add_route(ctx, "web-1")

    state.swap_in_route(*_features(ctx, route))
    state.swap_in_route(*_features(ctx, route))  # re-add: no duplicate
    data = state.data
    assert data is not None and data is not before
    assert [f.properties.id for f in data.routes_fc.features].count("web-1") == 1
    assert [f.properties.id for f in data.pins_fc.features].count("web-1-start") == 1
    # The previous snapshot is never mutated in place (lock-free readers).
    assert _ids(before.routes_fc) == {"r1"}

    state.swap_out_route("web-1")
    data = state.data
    assert data is not None
    assert _ids(data.routes_fc) == {"r1"}
    assert _ids(data.pins_fc) == {"pin-1", "r1-start"}


def test_update_route_properties_keeps_geometry(
    ctx: AppContext, state: UserState
) -> None:
    _add_pin(ctx, "pin-1")
    route = _add_route(ctx, "r1")
    before = userdata.ensure_loaded(ctx, state)
    edited = route.model_copy(update={"name": "Edited", "tags": ["x"]})

    state.update_route_properties(lambda: route_properties(edited))
    data = state.data
    assert data is not None and data is not before
    (line,) = data.routes_fc.features
    start = next(f for f in data.pins_fc.features if f.properties.id == "r1-start")
    assert (line.properties.name, line.properties.tags) == ("Edited", ["x"])
    assert (start.properties.name, start.properties.kind) == ("Edited", "route_start")
    assert line.geometry == before.routes_fc.features[0].geometry
    # The user's own pin is left alone; the old snapshot is not mutated.
    own_pin = next(f for f in data.pins_fc.features if f.properties.id == "pin-1")
    assert own_pin.properties.name == "Pin"
    assert before.routes_fc.features[0].properties.name == "Route"


def test_update_route_properties_skips_a_deleted_route(
    ctx: AppContext, state: UserState
) -> None:
    _add_route(ctx, "r1")
    before = userdata.ensure_loaded(ctx, state)
    state.update_route_properties(lambda: None)  # deleted since the edit
    assert state.data is before


def test_pin_swaps(ctx: AppContext, state: UserState) -> None:
    _add_pin(ctx, "pin-1")
    route = _add_route(ctx, "r1")
    state.refresh_pin("pin-2", lambda: _pin_feature("pin-2"))  # cold: no-op
    assert state.data is None
    userdata.ensure_loaded(ctx, state)

    state.refresh_pin("pin-2", lambda: _pin_feature("pin-2"))
    state.refresh_pin("pin-2", lambda: _pin_feature("pin-2", name="Renamed"))
    pins = _warm_pins(state)
    assert [f.properties.id for f in pins].count("pin-2") == 1
    assert next(f for f in pins if f.properties.id == "pin-2").properties.name == "Renamed"

    state.swap_out_pin("pin-2")
    # Only a pin of the user's is ever removed, never a route's start pin.
    state.swap_out_pin(f"{route.id}-start")
    assert {f.properties.id for f in _warm_pins(state)} == {"pin-1", "r1-start"}


def test_refresh_pin_never_restores_a_deleted_pin(
    ctx: AppContext, state: UserState
) -> None:
    _add_pin(ctx, "pin-1")
    userdata.ensure_loaded(ctx, state)
    # An edit's swap that runs after a concurrent DELETE committed and
    # swapped the pin out: the re-read finds no row, so the pin stays gone.
    state.swap_out_pin("pin-1")
    state.refresh_pin("pin-1", lambda: None)
    assert _warm_pins(state) == []


def _warm_pins(state: UserState) -> list[Any]:
    data = state.data
    assert data is not None
    return data.pins_fc.features


def _pin_feature(pin_id: str, name: str = "Pin") -> Any:
    return pin_to_feature(
        PinConfig(
            id=pin_id,
            name=name,
            lat=1,
            lon=2,
            sport=["hiking"],
            status="idea",  # type: ignore[arg-type]
        )
    )


# ── StateRegistry ────────────────────────────────────────────────────


def test_registry_returns_one_state_per_user() -> None:
    registry = StateRegistry()
    assert registry.get(1) is registry.get(1)
    assert registry.get(1) is not registry.get(2)
    assert registry.get(2).user_id == 2


def test_users_have_independent_data(ctx: AppContext) -> None:
    alice, bob = ctx.registry.get(ALICE), ctx.registry.get(BOB)
    userdata.ensure_loaded(ctx, alice)
    assert alice.data is not None
    assert bob.data is None
    alice.invalidate()
    userdata.ensure_loaded(ctx, bob)
    assert alice.data is None and bob.data is not None


def test_registry_concurrent_get_creates_one_state() -> None:
    registry = StateRegistry()
    seen: list[UserState] = []
    start = threading.Barrier(8)

    def get() -> None:
        start.wait()
        seen.append(registry.get(7))

    threads = [threading.Thread(target=get) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(s is seen[0] for s in seen)


def test_user_dir_is_per_user(ctx: AppContext) -> None:
    assert ctx.user_dir(ALICE) == ctx.data_root / "users" / "1"
    assert ctx.user_dir(ALICE) != ctx.user_dir(BOB)


# ── Through the app ──────────────────────────────────────────────────


def test_startup_does_not_build(user_client: TestClient) -> None:
    assert user_state("alice").data is None
    assert user_client.get("/api/config").status_code == 200
    assert user_state("alice").data is None
    assert user_client.get("/api/pins").status_code == 200
    assert user_state("alice").data is not None


def test_each_user_builds_only_their_own(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    user_client.get("/api/routes")
    assert user_state("alice").data is not None
    assert user_state("bob").data is None


def test_get_returns_500_on_build_failure_then_recovers(
    user_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build = userdata.build

    def broken_build(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk on fire")

    monkeypatch.setattr(userdata, "build", broken_build)
    resp = user_client.get("/api/routes")
    assert resp.status_code == 500
    assert "disk on fire" in resp.json()["detail"]

    monkeypatch.setattr(userdata, "build", real_build)
    resp = user_client.get("/api/routes")
    assert resp.status_code == 200
    assert resp.json()["features"] == []


@pytest.mark.parametrize("wishmap_toml", [LEGACY_TOML])
def test_server_toml_pins_and_routes_are_not_served(
    install: Path, user_client: TestClient
) -> None:
    # A legacy single-user TOML still starts the server, but its pins and
    # routes belong to no account (an import moves them into one).
    assert user_client.get("/api/routes").json()["features"] == []
    assert user_client.get("/api/pins").json()["features"] == []
