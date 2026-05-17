"""HTTP tests for the pin endpoints: POST /api/pins, PATCH/DELETE /api/pins/{id}.

Run on the shared fixtures (conftest.py) as alice (`user_client`), with
bob (`second_user_client`) checking that one user's pins can neither be
seen nor touched by another. A second, entered `TestClient(app)`
simulates a restart (cold state; sessions survive in the DB).
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import MINIMAL_GPX, ctx, user_id, user_state
from wishmap import pins_store, routes_store
from wishmap.app import app
from wishmap.models import PinConfig, RouteConfig

HUT: dict[str, Any] = {
    "name": "Hut",
    "lat": 46.5,
    "lon": 9.5,
    "sport": ["hiking"],
    "status": "idea",
    "tags": ["food"],
    "notes": "open in summer",
}


def _add(c: TestClient, **fields: Any) -> dict[str, Any]:
    resp = c.post("/api/pins", json={**HUT, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _pins(c: TestClient) -> dict[str, dict[str, Any]]:
    """The user's own pins (not route-start pins) from GET /api/pins, by id."""
    return {
        f["properties"]["id"]: f
        for f in c.get("/api/pins").json()["features"]
        if f["properties"]["kind"] == "pin"
    }


# ── POST ─────────────────────────────────────────────────────────────


def test_add_returns_the_feature(user_client: TestClient) -> None:
    feature = _add(user_client)
    props = feature["properties"]
    assert props["id"].startswith("pin-")
    assert props == {
        "id": props["id"],
        "name": "Hut",
        "kind": "pin",
        "sport": ["hiking"],
        "status": "idea",
        "tags": ["food"],
        "notes": "open in summer",
        "strava_id": None,
    }
    assert feature["geometry"] == {"type": "Point", "coordinates": [9.5, 46.5]}
    stored = pins_store.get(ctx().db_path, user_id("alice"), props["id"])
    assert stored is not None and stored.name == "Hut"


def test_added_pin_is_served_warm_and_cold(user_client: TestClient) -> None:
    assert _pins(user_client) == {}  # builds alice's map: warm from here on
    pin_id = _add(user_client)["properties"]["id"]
    assert set(_pins(user_client)) == {pin_id}  # swapped into the warm map
    with TestClient(app) as fresh:  # restart: built from the DB
        fresh.cookies.update(user_client.cookies)
        assert set(_pins(fresh)) == {pin_id}


def test_each_add_gets_a_new_id(user_client: TestClient) -> None:
    ids = {_add(user_client)["properties"]["id"] for _ in range(3)}
    assert len(ids) == 3


def test_add_minimal_body_defaults(user_client: TestClient) -> None:
    resp = user_client.post(
        "/api/pins",
        json={"name": " Lake ", "lat": 0, "lon": 0, "sport": "sup", "status": "done"},
    )
    assert resp.status_code == 201
    props = resp.json()["properties"]
    assert (props["name"], props["sport"], props["tags"], props["notes"]) == (
        "Lake",
        ["sup"],
        [],
        "",
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"name": None},
        {"name": "   "},
        {"lat": 91},
        {"lat": -90.5},
        {"lon": 180.5},
        {"lat": "north"},
        {"sport": []},
        {"sport": ["  "]},
        {"status": "someday"},
        {"tags": ["x" * 65]},
        {"id": "my-own-id"},  # the server picks ids
    ],
)
def test_add_rejects_bad_input(user_client: TestClient, bad: dict[str, Any]) -> None:
    resp = user_client.post("/api/pins", json={**HUT, **bad})
    assert resp.status_code == 422, resp.text
    assert pins_store.get_all(ctx().db_path, user_id("alice")) == []


@pytest.mark.parametrize("missing", ["name", "lat", "lon", "sport", "status"])
def test_add_requires_field(user_client: TestClient, missing: str) -> None:
    body = {k: v for k, v in HUT.items() if k != missing}
    assert user_client.post("/api/pins", json=body).status_code == 422


# ── PATCH ────────────────────────────────────────────────────────────


def test_patch_changes_only_the_given_fields(user_client: TestClient) -> None:
    pin_id = _add(user_client)["properties"]["id"]
    _pins(user_client)  # warm
    resp = user_client.patch(f"/api/pins/{pin_id}", json={"name": "New Hut", "status": "done"})
    assert resp.status_code == 200, resp.text
    props = resp.json()["properties"]
    assert (props["name"], props["status"]) == ("New Hut", "done")
    assert (props["sport"], props["tags"], props["notes"]) == (
        ["hiking"],
        ["food"],
        "open in summer",
    )
    assert _pins(user_client)[pin_id]["properties"] == props
    stored = pins_store.get(ctx().db_path, user_id("alice"), pin_id)
    assert stored is not None and stored.name == "New Hut" and stored.lat == 46.5


def test_patch_moves_the_pin(user_client: TestClient) -> None:
    pin_id = _add(user_client)["properties"]["id"]
    resp = user_client.patch(f"/api/pins/{pin_id}", json={"lat": 47.0, "lon": 8.25})
    assert resp.json()["geometry"]["coordinates"] == [8.25, 47.0]
    assert _pins(user_client)[pin_id]["geometry"]["coordinates"] == [8.25, 47.0]


def test_patch_can_clear_tags_and_notes(user_client: TestClient) -> None:
    pin_id = _add(user_client)["properties"]["id"]
    props = user_client.patch(f"/api/pins/{pin_id}", json={"tags": [], "notes": ""}).json()[
        "properties"
    ]
    assert (props["tags"], props["notes"]) == ([], "")


@pytest.mark.parametrize(
    "bad",
    [
        {"name": None},
        {"name": ""},
        {"lat": None},
        {"lat": 100},
        {"sport": []},
        {"status": "nope"},
        {"id": "other"},
    ],
)
def test_patch_rejects_bad_input(user_client: TestClient, bad: dict[str, Any]) -> None:
    pin_id = _add(user_client)["properties"]["id"]
    resp = user_client.patch(f"/api/pins/{pin_id}", json=bad)
    assert resp.status_code == 422, resp.text
    stored = pins_store.get(ctx().db_path, user_id("alice"), pin_id)
    assert stored is not None and stored.name == "Hut" and stored.lat == 46.5


def test_patch_unknown_pin_is_404(user_client: TestClient) -> None:
    resp = user_client.patch("/api/pins/pin-nope", json={"name": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown pin_id"


def test_patch_racing_a_delete_never_restores_the_pin(
    user_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A DELETE that commits and swaps the pin out between the PATCH's DB
    # write and its swap: the PATCH's swap must not put the pin back.
    pin_id = _add(user_client)["properties"]["id"]
    _pins(user_client)  # warm
    real_update = pins_store.update

    def update_then_delete(*args: Any, **kwargs: Any) -> PinConfig | None:
        pin = real_update(*args, **kwargs)
        assert pins_store.delete(ctx().db_path, user_id("alice"), pin_id)
        user_state("alice").swap_out_pin(pin_id)
        return pin

    monkeypatch.setattr(pins_store, "update", update_then_delete)
    assert user_client.patch(f"/api/pins/{pin_id}", json={"name": "x"}).status_code == 200
    assert _pins(user_client) == {}


def test_patch_swaps_in_the_latest_commit(
    user_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second edit that commits between this PATCH's DB write and its
    # swap wins on the map, as it does in the DB.
    pin_id = _add(user_client)["properties"]["id"]
    _pins(user_client)  # warm
    real_update = pins_store.update

    def update_then_edit_again(*args: Any, **kwargs: Any) -> PinConfig | None:
        pin = real_update(*args, **kwargs)
        real_update(ctx().db_path, user_id("alice"), pin_id, {"name": "Later"})
        return pin

    monkeypatch.setattr(pins_store, "update", update_then_edit_again)
    resp = user_client.patch(f"/api/pins/{pin_id}", json={"name": "Earlier"})
    assert resp.json()["properties"]["name"] == "Earlier"
    assert _pins(user_client)[pin_id]["properties"]["name"] == "Later"


# ── DELETE ───────────────────────────────────────────────────────────


def test_delete_removes_the_pin(user_client: TestClient) -> None:
    keep = _add(user_client, name="Keep")["properties"]["id"]
    gone = _add(user_client, name="Gone")["properties"]["id"]
    _pins(user_client)  # warm
    assert user_client.delete(f"/api/pins/{gone}").status_code == 204
    assert set(_pins(user_client)) == {keep}
    assert pins_store.get(ctx().db_path, user_id("alice"), gone) is None
    # A second delete finds nothing.
    assert user_client.delete(f"/api/pins/{gone}").status_code == 404


def test_delete_never_touches_route_start_pins(user_client: TestClient) -> None:
    # A derived route-start pin isn't a pin of the user's: deleting its id
    # 404s and the route's start stays on the map.
    uid = user_id("alice")
    rel = "gpx/web/web-1.gpx"
    (ctx().user_dir(uid) / rel).parent.mkdir(parents=True)
    (ctx().user_dir(uid) / rel).write_text(MINIMAL_GPX)
    routes_store.insert(
        ctx().db_path,
        uid,
        RouteConfig(id="web-1", name="R", sport=["hiking"], status="idea", gpx=rel),  # type: ignore[arg-type]
    )
    assert user_client.delete("/api/pins/web-1-start").status_code == 404
    ids = {f["properties"]["id"] for f in user_client.get("/api/pins").json()["features"]}
    assert ids == {"web-1-start"}


# ── Isolation between users ──────────────────────────────────────────


def test_users_only_see_their_own_pins(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    alice_pin = _add(user_client, name="Alice's")["properties"]["id"]
    bob_pin = _add(second_user_client, name="Bob's")["properties"]["id"]
    assert set(_pins(user_client)) == {alice_pin}
    assert set(_pins(second_user_client)) == {bob_pin}


def test_users_cannot_touch_each_others_pins(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    alice_pin = _add(user_client)["properties"]["id"]
    _pins(user_client)  # warm alice's map
    resp = second_user_client.patch(f"/api/pins/{alice_pin}", json={"name": "Bob was here"})
    assert resp.status_code == 404
    assert second_user_client.delete(f"/api/pins/{alice_pin}").status_code == 404
    assert _pins(user_client)[alice_pin]["properties"]["name"] == "Hut"
    assert _pins(second_user_client) == {}


def test_same_pin_id_is_independent_per_user(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    # Ids are only unique per user: bob owning the same id (e.g. from an
    # import) doesn't let alice's request reach his row.
    pin = PinConfig(
        id="pin-shared", name="Bob's", lat=1, lon=1, sport=["hiking"], status="idea"  # type: ignore[arg-type]
    )
    pins_store.insert(ctx().db_path, user_id("bob"), pin)
    assert user_client.patch("/api/pins/pin-shared", json={"name": "x"}).status_code == 404
    assert user_client.delete("/api/pins/pin-shared").status_code == 404
    stored = pins_store.get(ctx().db_path, user_id("bob"), "pin-shared")
    assert stored is not None and stored.name == "Bob's"
