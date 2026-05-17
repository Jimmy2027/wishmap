"""HTTP integration tests for the web-route add/delete endpoints.

Run on the shared fixtures (conftest.py): `user_client` is alice, signed
in on a tmp_path install; `second_user_client` (bob) checks that one
user's routes and files never leak into another's. Uploaded GPX lands in
the user's own dir, `data/users/<id>/gpx/web/`. A second, entered
`TestClient(app)` simulates a restart: its lifespan starts a fresh, cold
state registry (session cookies survive — sessions live in the DB).
"""

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ctx, user_id, user_state
from wishmap import app as app_module
from wishmap import routes_store
from wishmap.app import app
from wishmap.auth import SESSION_COOKIE
from wishmap.models import RouteConfig
from wishmap.state import UserState

# A second GPX with different coordinates — used to prove a re-add updates the
# stored geometry in place (first trkpt 47.5/8.5 instead of 46.9/9.7).
ALT_GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="StravaGPX" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Alt Trail</name><trkseg>
    <trkpt lat="47.5" lon="8.5"/>
    <trkpt lat="47.51" lon="8.51"/>
  </trkseg></trk>
</gpx>
"""

MINIMAL_GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="StravaGPX" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Uploaded Trail</name><trkseg>
    <trkpt lat="46.9" lon="9.7"/>
    <trkpt lat="46.91" lon="9.71"/>
  </trkseg></trk>
</gpx>
"""

EMPTY_GPX = (
    '<?xml version="1.0"?>'
    '<gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1"></gpx>'
)



def _state() -> UserState:
    """Alice's state in the running app."""
    return user_state("alice")


def _web_gpx_dir(username: str = "alice") -> Path:
    return ctx().user_dir(user_id(username)) / "gpx" / "web"


def _seed_route(
    route_id: str, source: routes_store.Source, gpx_text: str = MINIMAL_GPX
) -> None:
    """Give alice a non-web route (as an import or a Garmin sync would)."""
    uid = user_id("alice")
    rel = f"gpx/{source}/{route_id}.gpx"
    (ctx().user_dir(uid) / rel).parent.mkdir(parents=True, exist_ok=True)
    (ctx().user_dir(uid) / rel).write_text(gpx_text)
    routes_store.insert(
        ctx().db_path,
        uid,
        RouteConfig(
            id=route_id,
            name="Seeded",
            sport=["hiking"],
            status="done",  # type: ignore[arg-type]
            gpx=rel,
        ),
        source=source,
    )


def _ids(c: TestClient, kind: str) -> set[str]:
    return {f["properties"]["id"] for f in c.get(f"/api/{kind}").json()["features"]}


@pytest.fixture
def client(user_client: TestClient) -> TestClient:
    """This module acts as alice throughout."""
    return user_client


def _add_route(
    client: TestClient,
    *,
    strava_url: str = "https://www.strava.com/routes/123",
    name: str = "My Route",
    sport: str = "biking",
    status: str = "idea",
    tags: str = "loop, gravel",
    gpx_content: str = MINIMAL_GPX,
    gpx_name: str = "route.gpx",
    headers: dict[str, str] | None = None,
) -> Any:
    return client.post(
        "/api/routes",
        data={
            "strava_url": strava_url,
            "name": name,
            "sport": sport,
            "status": status,
            "tags": tags,
        },
        files={"gpx_file": (gpx_name, gpx_content, "application/gpx+xml")},
        headers=headers or {},
    )


# ── Happy path ───────────────────────────────────────────────────────


def test_add_route_happy_201(client: TestClient) -> None:
    resp = _add_route(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["created"] is True
    assert body["route"]["properties"]["id"] == "web-strava-123"
    assert body["route"]["geometry"]["type"] == "LineString"
    assert body["start"]["properties"]["id"] == "web-strava-123-start"
    assert body["start"]["geometry"]["type"] == "Point"
    # Geometry values: GeoJSON order is [lon, lat]; the start pin is the first
    # GPX coord. MINIMAL_GPX's first trkpt is lat=46.9 lon=9.7.
    assert body["route"]["geometry"]["coordinates"][0] == [9.7, 46.9]
    assert body["route"]["geometry"]["coordinates"][1] == [9.71, 46.91]
    assert body["start"]["geometry"]["coordinates"] == [9.7, 46.9]

    routes = client.get("/api/routes").json()
    assert any(f["properties"]["id"] == "web-strava-123" for f in routes["features"])
    pins = client.get("/api/pins").json()
    assert any(
        f["properties"]["id"] == "web-strava-123-start" for f in pins["features"]
    )


def test_add_route_stores_url_in_notes_and_parses_tags(client: TestClient) -> None:
    body = _add_route(client, tags="a, b ,c").json()
    props = body["route"]["properties"]
    assert props["notes"] == "https://www.strava.com/routes/123"
    assert props["tags"] == ["a", "b", "c"]
    # strava_id is for activity deep-links, not routes — never set here.
    assert props["strava_id"] is None


def test_add_route_gpx_written_to_users_dir(client: TestClient, tmp_path: Path) -> None:
    body = _add_route(client).json()
    user_dir = tmp_path / "data" / "users" / str(user_id("alice"))
    assert (user_dir / "gpx" / "web" / "web-strava-123.gpx").is_file()
    # Stored relative to the user's dir, so the tree can move as a whole.
    route = routes_store.get(ctx().db_path, user_id("alice"), "web-strava-123")
    assert route is not None and route.gpx == "gpx/web/web-strava-123.gpx"
    assert body["created"] is True


def test_other_routes_unaffected_by_add(client: TestClient) -> None:
    _seed_route("import-1", "import")
    _add_route(client)
    assert {"import-1", "web-strava-123"} <= _ids(client, "routes")


# ── id derivation ────────────────────────────────────────────────────


def test_add_route_no_parseable_id_uses_uuid(client: TestClient) -> None:
    resp = _add_route(client, strava_url="https://example.com/foo")
    assert resp.status_code == 201
    rid = resp.json()["route"]["properties"]["id"]
    assert rid.startswith("web-")
    assert not rid.startswith("web-strava-")


def test_add_route_url_traversal_neutralized(client: TestClient) -> None:
    # A crafted URL must not smuggle path separators into the GPX filename.
    resp = _add_route(
        client, strava_url="https://www.strava.com/routes/../../etc/passwd"
    )
    assert resp.status_code == 201
    rid = resp.json()["route"]["properties"]["id"]
    assert "/" not in rid and ".." not in rid
    gpx_dir = _web_gpx_dir()
    written = list(gpx_dir.glob("*.gpx"))
    assert len(written) == 1
    assert written[0].parent == gpx_dir


# ── Validation / edge cases ──────────────────────────────────────────


def test_add_route_empty_gpx_422_nothing_written(client: TestClient) -> None:
    resp = _add_route(client, gpx_content=EMPTY_GPX)
    assert resp.status_code == 422
    routes = client.get("/api/routes").json()
    assert all(
        not f["properties"]["id"].startswith("web-") for f in routes["features"]
    )
    # No GPX left on disk for the rejected upload.
    assert not (_web_gpx_dir() / "web-strava-123.gpx").exists()


def test_add_route_garbage_gpx_422(client: TestClient) -> None:
    resp = _add_route(client, gpx_content="this is not xml at all")
    assert resp.status_code == 422


def test_add_route_bad_status_422(client: TestClient) -> None:
    resp = _add_route(client, status="banana")
    assert resp.status_code == 422


def test_add_route_missing_name_422(client: TestClient) -> None:
    resp = client.post(
        "/api/routes",
        data={
            "strava_url": "https://www.strava.com/routes/123",
            "sport": "biking",
            "status": "idea",
        },
        files={"gpx_file": ("route.gpx", MINIMAL_GPX, "application/gpx+xml")},
    )
    assert resp.status_code == 422


def test_add_route_oversize_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "MAX_GPX_BYTES", 50)
    resp = _add_route(client, gpx_content="x" * 200)
    assert resp.status_code == 413


def test_add_route_no_file_422(client: TestClient) -> None:
    # gpx_file is File(...) — a request with no file part is rejected.
    resp = client.post(
        "/api/routes",
        data={
            "strava_url": "https://www.strava.com/routes/123",
            "name": "x",
            "sport": "biking",
            "status": "idea",
        },
    )
    assert resp.status_code == 422


def test_add_route_empty_bytes_gpx_422(client: TestClient) -> None:
    resp = _add_route(client, gpx_content="")
    assert resp.status_code == 422
    assert not (_web_gpx_dir() / "web-strava-123.gpx").exists()


def test_add_route_json_body_422(client: TestClient) -> None:
    resp = client.post("/api/routes", json={"name": "x", "sport": "biking"})
    assert resp.status_code == 422


# ── Sec-Fetch-Site guard ─────────────────────────────────────────────


def test_add_route_cross_origin_403(client: TestClient) -> None:
    resp = _add_route(client, headers={"sec-fetch-site": "cross-site"})
    assert resp.status_code == 403
    assert not _web_gpx_dir().exists()


def test_add_route_same_origin_allowed(client: TestClient) -> None:
    resp = _add_route(client, headers={"sec-fetch-site": "same-origin"})
    assert resp.status_code == 201


# ── Re-add (INSERT OR REPLACE) ───────────────────────────────────────


def test_re_add_same_route_replaces_in_place(client: TestClient) -> None:
    r1 = _add_route(client, name="First")
    assert r1.status_code == 201 and r1.json()["created"] is True
    r2 = _add_route(client, name="Second", status="done")
    assert r2.status_code == 201 and r2.json()["created"] is False

    routes = client.get("/api/routes").json()
    matches = [f for f in routes["features"] if f["properties"]["id"] == "web-strava-123"]
    assert len(matches) == 1  # no duplicate line
    assert matches[0]["properties"]["name"] == "Second"

    pins = client.get("/api/pins").json()
    starts = [
        f for f in pins["features"] if f["properties"]["id"] == "web-strava-123-start"
    ]
    assert len(starts) == 1  # no duplicate start pin


def test_re_add_with_different_gpx_updates_geometry(client: TestClient) -> None:
    _add_route(client, gpx_content=MINIMAL_GPX)
    _add_route(client, gpx_content=ALT_GPX)  # same id (same strava url), new GPX
    routes = client.get("/api/routes").json()
    feat = next(
        f for f in routes["features"] if f["properties"]["id"] == "web-strava-123"
    )
    # Geometry must reflect the NEW GPX (first trkpt 47.5/8.5), not the old one.
    assert feat["geometry"]["coordinates"][0] == [8.5, 47.5]
    pins = client.get("/api/pins").json()
    start = next(
        f for f in pins["features"] if f["properties"]["id"] == "web-strava-123-start"
    )
    assert start["geometry"]["coordinates"] == [8.5, 47.5]


# ── DELETE ───────────────────────────────────────────────────────────


def test_delete_route_204_removes_everything(client: TestClient) -> None:
    _add_route(client)
    gpx_file = _web_gpx_dir() / "web-strava-123.gpx"
    assert gpx_file.exists()

    resp = client.delete("/api/routes/web-strava-123")
    assert resp.status_code == 204

    routes = client.get("/api/routes").json()
    assert all(f["properties"]["id"] != "web-strava-123" for f in routes["features"])
    pins = client.get("/api/pins").json()
    assert all(
        f["properties"]["id"] != "web-strava-123-start" for f in pins["features"]
    )
    assert not gpx_file.exists()  # GPX cleaned up


def test_delete_uuid_id_route_204(client: TestClient) -> None:
    # The uuid-fallback id branch must delete just like a web-strava-<digits> id.
    rid = _add_route(client, strava_url="https://example.com/foo").json()[
        "route"
    ]["properties"]["id"]
    assert rid.startswith("web-") and not rid.startswith("web-strava-")
    assert client.delete(f"/api/routes/{rid}").status_code == 204
    routes = client.get("/api/routes").json()
    assert all(f["properties"]["id"] != rid for f in routes["features"])


def test_delete_keeps_sibling_web_route(client: TestClient) -> None:
    _add_route(client, strava_url="https://www.strava.com/routes/123")
    _add_route(client, strava_url="https://www.strava.com/routes/456")
    assert client.delete("/api/routes/web-strava-123").status_code == 204

    routes = {f["properties"]["id"] for f in client.get("/api/routes").json()["features"]}
    assert "web-strava-456" in routes and "web-strava-123" not in routes
    pins = {f["properties"]["id"] for f in client.get("/api/pins").json()["features"]}
    assert "web-strava-456-start" in pins and "web-strava-123-start" not in pins


def test_delete_unknown_404(client: TestClient) -> None:
    assert client.delete("/api/routes/web-nope").status_code == 404


@pytest.mark.parametrize("source", ["import", "garmin"])
def test_delete_any_owned_route(client: TestClient, source: routes_store.Source) -> None:
    # Not just web routes: every route in the account is the user's to delete.
    _seed_route("seeded-1", source)
    assert "seeded-1" in _ids(client, "routes")
    assert client.delete("/api/routes/seeded-1").status_code == 204
    assert "seeded-1" not in _ids(client, "routes")
    assert not (ctx().user_dir(user_id("alice")) / "gpx" / source / "seeded-1.gpx").exists()


def test_delete_never_unlinks_outside_users_dir(client: TestClient, tmp_path: Path) -> None:
    # A route whose stored gpx path escapes the user's dir loses its row,
    # but the file outside stays put.
    outside = tmp_path / "precious.gpx"
    outside.write_text(MINIMAL_GPX)
    routes_store.insert(
        ctx().db_path,
        user_id("alice"),
        RouteConfig(
            id="evil",
            name="x",
            sport=["hiking"],
            status="idea",  # type: ignore[arg-type]
            gpx="../../../precious.gpx",
        ),
    )
    assert client.delete("/api/routes/evil").status_code == 204
    assert outside.exists()
    assert routes_store.get(ctx().db_path, user_id("alice"), "evil") is None


def test_delete_cross_origin_403(client: TestClient) -> None:
    _add_route(client)
    resp = client.delete(
        "/api/routes/web-strava-123", headers={"sec-fetch-site": "cross-site"}
    )
    assert resp.status_code == 403


def _restarted_client(signed_in: TestClient) -> TestClient:
    """A client on a freshly restarted app, carrying the same session cookie."""
    c2 = TestClient(app)
    c2.cookies.set(SESSION_COOKIE, signed_in.cookies[SESSION_COOKIE])
    return c2


def test_delete_survives_restart(client: TestClient) -> None:
    _add_route(client)
    client.delete("/api/routes/web-strava-123")
    with _restarted_client(client) as c2:
        routes = c2.get("/api/routes").json()
        assert all(
            f["properties"]["id"] != "web-strava-123" for f in routes["features"]
        )


# ── R1 regression (IRON RULE): web routes must be ratable ────────────


def test_rate_web_route_returns_200(client: TestClient) -> None:
    _add_route(client)
    resp = client.put("/api/ratings/web-strava-123", json={"fun": 5})
    assert resp.status_code == 200
    assert resp.json()["fun"] == 5


def test_rate_unknown_route_still_404(client: TestClient) -> None:
    assert client.put("/api/ratings/web-nope", json={"fun": 5}).status_code == 404


# ── Persistence / resilience ─────────────────────────────────────────


def test_web_route_survives_restart(client: TestClient) -> None:
    _add_route(client)
    # Simulate a restart: a fresh lifespan starts cold, so the first GET
    # re-reads the DB. WISHMAP_CONFIG still points at the same install, and
    # the session (in the DB) still signs alice in.
    with _restarted_client(client) as c2:
        routes = c2.get("/api/routes").json()
        assert any(
            f["properties"]["id"] == "web-strava-123" for f in routes["features"]
        )


def test_first_build_skips_route_with_missing_gpx(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_route("import-1", "import")
    # Seed a web route whose GPX file does NOT exist on disk.
    routes_store.insert(
        ctx().db_path,
        user_id("alice"),
        RouteConfig(
            id="web-strava-999",
            name="Ghost",
            sport=["biking"],
            status="idea",  # type: ignore[arg-type]
            gpx="gpx/web/web-strava-999.gpx",
        ),
    )
    # The first build must NOT fail even though the web route's GPX is missing.
    with caplog.at_level(logging.WARNING, logger="wishmap.userdata"):
        ids = _ids(client, "routes")
    assert "web-strava-999" not in ids  # ghost skipped
    assert "import-1" in ids  # the healthy route still renders
    assert any("web-strava-999" in r.message for r in caplog.records)


def test_rebuild_after_invalidate_skips_web_route_with_missing_gpx(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # The skip must also hold on the sync path (invalidate + rebuild on the
    # next GET), not just on the first build after startup.
    _seed_route("import-1", "import")
    _add_route(client)
    client.get("/api/routes")  # warm
    (_web_gpx_dir() / "web-strava-123.gpx").unlink()
    _state().invalidate()
    with caplog.at_level(logging.WARNING, logger="wishmap.userdata"):
        resp = client.get("/api/routes")  # must not fail
    assert resp.status_code == 200
    routes = {f["properties"]["id"] for f in resp.json()["features"]}
    assert "web-strava-123" not in routes  # ghost skipped on rebuild
    assert "import-1" in routes  # the healthy route still renders
    assert any("web-strava-123" in r.message for r in caplog.records)


# ── PATCH (edit) ─────────────────────────────────────────────────────


def _features_by_id(c: TestClient, kind: str) -> dict[str, Any]:
    return {f["properties"]["id"]: f for f in c.get(f"/api/{kind}").json()["features"]}


def test_patch_route_edits_line_and_start_pin(client: TestClient) -> None:
    _add_route(client, name="Old", sport="biking", status="idea", tags="loop")
    client.get("/api/routes")  # warm: the edit is swapped into the built map
    resp = client.patch(
        "/api/routes/web-strava-123",
        json={
            "name": "New",
            "sport": ["hiking", "walking"],
            "status": "done",
            "tags": ["summit"],
            "notes": "great views",
        },
    )
    assert resp.status_code == 200, resp.text
    props = resp.json()
    assert props == {
        "id": "web-strava-123",
        "name": "New",
        "kind": "route",
        "sport": ["hiking", "walking"],
        "status": "done",
        "tags": ["summit"],
        "notes": "great views",
        "strava_id": None,
    }
    line = _features_by_id(client, "routes")["web-strava-123"]
    start = _features_by_id(client, "pins")["web-strava-123-start"]
    assert line["properties"] == props
    assert start["properties"] == {**props, "id": "web-strava-123-start", "kind": "route_start"}
    # The geometry is untouched.
    assert line["geometry"]["coordinates"] == [[9.7, 46.9], [9.71, 46.91]]
    assert start["geometry"]["coordinates"] == [9.7, 46.9]
    stored = routes_store.get(ctx().db_path, user_id("alice"), "web-strava-123")
    assert stored is not None
    assert (stored.name, stored.status.value, stored.gpx) == (
        "New",
        "done",
        "gpx/web/web-strava-123.gpx",
    )


def test_patch_route_keeps_absent_fields(client: TestClient) -> None:
    _add_route(client, name="Keep", sport="biking", tags="a, b")
    props = client.patch("/api/routes/web-strava-123", json={"status": "planned"}).json()
    assert (props["name"], props["sport"], props["tags"], props["status"]) == (
        "Keep",
        ["biking"],
        ["a", "b"],
        "planned",
    )
    assert props["notes"] == "https://www.strava.com/routes/123"


def test_patch_route_on_cold_state_does_not_build(client: TestClient) -> None:
    _add_route(client)
    assert _state().data is None
    assert client.patch("/api/routes/web-strava-123", json={"name": "Cold"}).status_code == 200
    assert _state().data is None
    assert _features_by_id(client, "routes")["web-strava-123"]["properties"]["name"] == "Cold"


@pytest.mark.parametrize("source", ["garmin", "import"])
def test_patch_any_owned_route(client: TestClient, source: routes_store.Source) -> None:
    _seed_route(f"{source}-1", source)
    resp = client.patch(f"/api/routes/{source}-1", json={"status": "planned"})
    assert resp.status_code == 200
    stored = routes_store.get(ctx().db_path, user_id("alice"), f"{source}-1")
    assert stored is not None and stored.status.value == "planned"


def test_patch_route_swaps_in_the_latest_commit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second edit that commits between this PATCH's DB write and its
    # swap wins on the map, as it does in the DB.
    _add_route(client)
    client.get("/api/routes")  # warm
    real_update = routes_store.update

    def update_then_edit_again(*args: Any, **kwargs: Any) -> RouteConfig | None:
        route = real_update(*args, **kwargs)
        real_update(ctx().db_path, user_id("alice"), "web-strava-123", {"name": "Later"})
        return route

    monkeypatch.setattr(routes_store, "update", update_then_edit_again)
    resp = client.patch("/api/routes/web-strava-123", json={"name": "Earlier"})
    assert resp.json()["name"] == "Earlier"
    line = _features_by_id(client, "routes")["web-strava-123"]
    start = _features_by_id(client, "pins")["web-strava-123-start"]
    assert line["properties"]["name"] == start["properties"]["name"] == "Later"


def test_patch_route_keeps_its_rating(client: TestClient) -> None:
    _add_route(client)
    client.put("/api/ratings/web-strava-123", json={"fun": 5})
    client.patch("/api/routes/web-strava-123", json={"name": "Renamed"})
    assert client.get("/api/ratings").json()["web-strava-123"]["fun"] == 5


@pytest.mark.parametrize(
    "bad",
    [
        {"name": None},
        {"name": "  "},
        {"status": "someday"},
        {"sport": []},
        {"tags": None},
        # Identity and geometry are not editable.
        {"id": "other"},
        {"gpx": "../../elsewhere.gpx"},
        {"strava_id": 1},
    ],
)
def test_patch_route_rejects_bad_input(client: TestClient, bad: dict[str, Any]) -> None:
    _add_route(client, name="Same")
    resp = client.patch("/api/routes/web-strava-123", json=bad)
    assert resp.status_code == 422, resp.text
    stored = routes_store.get(ctx().db_path, user_id("alice"), "web-strava-123")
    assert stored is not None
    assert (stored.name, stored.gpx, stored.strava_id) == (
        "Same",
        "gpx/web/web-strava-123.gpx",
        None,
    )


def test_patch_unknown_route_404(client: TestClient) -> None:
    resp = client.patch("/api/routes/nope", json={"name": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown route_id"


def test_patch_cross_origin_403(client: TestClient) -> None:
    _add_route(client, name="Same")
    resp = client.patch(
        "/api/routes/web-strava-123",
        json={"name": "x"},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert resp.status_code == 403
    assert _features_by_id(client, "routes")["web-strava-123"]["properties"]["name"] == "Same"


# ── Lazy map data: swaps on a cold vs warm state ─────────────────────


def test_add_on_cold_state_does_not_build_and_shows_on_first_get(
    client: TestClient,
) -> None:
    # Nothing has read the map yet, so the add must not trigger a full
    # build; the route lands in the DB and the first GET builds it in.
    _seed_route("import-1", "import")
    assert _state().data is None
    assert _add_route(client).status_code == 201
    assert _state().data is None
    assert {"import-1", "web-strava-123"} <= _ids(client, "routes")


def test_add_and_delete_on_warm_state_swap_without_rebuild(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_route("import-1", "import")
    client.get("/api/routes")  # warm
    assert _state().data is not None

    def no_rebuild(*args: object) -> None:
        raise AssertionError("a warm add/delete must not rebuild the map")

    monkeypatch.setattr("wishmap.userdata.build", no_rebuild)

    _add_route(client)
    routes = {f["properties"]["id"] for f in client.get("/api/routes").json()["features"]}
    assert "web-strava-123" in routes
    pins = {f["properties"]["id"] for f in client.get("/api/pins").json()["features"]}
    assert "web-strava-123-start" in pins

    assert client.delete("/api/routes/web-strava-123").status_code == 204
    routes = _ids(client, "routes")
    assert "web-strava-123" not in routes and "import-1" in routes


def test_delete_and_rate_on_cold_state_do_not_build(client: TestClient) -> None:
    # Delete/rate only look up the one route in the DB; on a cold state
    # they must not pay for (or trigger) the full GPX build.
    _seed_route("import-1", "import")
    assert _add_route(client).status_code == 201
    assert _state().data is None
    assert client.put("/api/ratings/web-strava-123", json={"fun": 4}).status_code == 200
    assert client.put("/api/ratings/import-1", json={"fun": 3}).status_code == 200
    assert client.delete("/api/routes/import-1").status_code == 204
    assert client.delete("/api/routes/web-strava-123").status_code == 204
    assert _state().data is None


# ── Cross-user isolation ─────────────────────────────────────────────


def test_routes_are_invisible_to_other_users(
    client: TestClient, second_user_client: TestClient
) -> None:
    _add_route(client)
    client.get("/api/routes")  # warm alice's map too
    assert _ids(second_user_client, "routes") == set()
    assert _ids(second_user_client, "pins") == set()


def test_other_user_cannot_delete_or_rate(
    client: TestClient, second_user_client: TestClient
) -> None:
    _add_route(client)
    assert second_user_client.delete("/api/routes/web-strava-123").status_code == 404
    assert (
        second_user_client.put("/api/ratings/web-strava-123", json={"fun": 1}).status_code
        == 404
    )
    # Alice's route and file are untouched.
    assert "web-strava-123" in _ids(client, "routes")
    assert (_web_gpx_dir("alice") / "web-strava-123.gpx").exists()


def test_other_user_cannot_edit(
    client: TestClient, second_user_client: TestClient
) -> None:
    _add_route(client, name="Alice's")
    client.get("/api/routes")  # warm alice's map
    resp = second_user_client.patch("/api/routes/web-strava-123", json={"name": "Bob's"})
    assert resp.status_code == 404
    assert _features_by_id(client, "routes")["web-strava-123"]["properties"]["name"] == "Alice's"
    stored = routes_store.get(ctx().db_path, user_id("alice"), "web-strava-123")
    assert stored is not None and stored.name == "Alice's"


def test_edit_reaches_only_the_editors_copy(
    client: TestClient, second_user_client: TestClient
) -> None:
    # Both own a route with this id; bob's edit changes his alone.
    _add_route(client, name="Alice's")
    _add_route(second_user_client, name="Bob's")
    client.get("/api/routes")
    second_user_client.get("/api/routes")
    assert (
        second_user_client.patch("/api/routes/web-strava-123", json={"name": "Bob 2"}).status_code
        == 200
    )
    assert _features_by_id(client, "routes")["web-strava-123"]["properties"]["name"] == "Alice's"
    assert (
        _features_by_id(second_user_client, "routes")["web-strava-123"]["properties"]["name"]
        == "Bob 2"
    )


def test_same_route_id_in_two_accounts_is_independent(
    client: TestClient, second_user_client: TestClient
) -> None:
    assert _add_route(client, name="Alice's").json()["created"] is True
    # Bob adding the same Strava route creates his own copy, in his own dir.
    resp = _add_route(second_user_client, name="Bob's", gpx_content=ALT_GPX)
    assert resp.status_code == 201 and resp.json()["created"] is True
    assert (_web_gpx_dir("alice") / "web-strava-123.gpx").read_text() == MINIMAL_GPX
    assert (_web_gpx_dir("bob") / "web-strava-123.gpx").read_text() == ALT_GPX

    assert second_user_client.delete("/api/routes/web-strava-123").status_code == 204
    alice_routes = client.get("/api/routes").json()["features"]
    assert [f["properties"]["name"] for f in alice_routes] == ["Alice's"]
    assert (_web_gpx_dir("alice") / "web-strava-123.gpx").exists()
