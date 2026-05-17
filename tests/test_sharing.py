"""HTTP tests for sharing routes with the other accounts on the server.

Run on the shared fixtures (conftest.py): `user_client` is alice, who
shares, and `second_user_client` is bob, who overlays her routes; `anon`
is a further cookie jar with no session at all.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.helpers import MINIMAL_GPX, ctx, user_id
from wishmap import public_links, routes_store
from wishmap.app import app
from wishmap.models import RouteConfig


@pytest.fixture
def anon(client: TestClient) -> Iterator[TestClient]:
    c = TestClient(app)
    yield c
    c.close()


def _seed_route(username: str, route_id: str) -> None:
    """Give a user a renderable route with private fields set."""
    uid = user_id(username)
    rel = f"gpx/web/{route_id}.gpx"
    path = ctx().user_dir(uid) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MINIMAL_GPX)
    routes_store.insert(
        ctx().db_path,
        uid,
        RouteConfig(
            id=route_id,
            name=f"{username}'s route",
            sport=["hiking"],
            status="planned",  # type: ignore[arg-type]
            tags=["alps"],
            notes="the key is under the mat",
            gpx=rel,
            strava_id=12345,
        ),
    )


def _share(c: TestClient, enabled: bool) -> None:
    resp = c.put("/api/sharing", json={"enabled": enabled})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": enabled}


def _shared_routes_path(username: str) -> str:
    return f"/api/shared/{user_id(username)}/routes"


# ── /api/sharing and /api/me ─────────────────────────────────────────


def test_me_reports_sharing(user_client: TestClient) -> None:
    assert user_client.get("/api/me").json()["sharing"] is False
    _share(user_client, True)
    assert user_client.get("/api/me").json()["sharing"] is True
    _share(user_client, True)  # idempotent
    assert user_client.get("/api/me").json()["sharing"] is True
    _share(user_client, False)
    assert user_client.get("/api/me").json()["sharing"] is False


def test_put_requires_same_origin(user_client: TestClient) -> None:
    resp = user_client.put(
        "/api/sharing",
        json={"enabled": True},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert resp.status_code == 403
    assert user_client.get("/api/me").json()["sharing"] is False


def test_put_requires_auth(anon: TestClient) -> None:
    assert anon.put("/api/sharing", json={"enabled": True}).status_code == 401


def test_put_validates_body(user_client: TestClient) -> None:
    assert user_client.put("/api/sharing", json={}).status_code == 422


# ── /api/shared-users and /api/shared/<id>/routes ────────────────────


def test_sharer_is_listed_only_after_opting_in(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    assert second_user_client.get("/api/shared-users").json() == []
    _share(user_client, True)
    assert second_user_client.get("/api/shared-users").json() == [
        {"id": user_id("alice"), "username": "alice"}
    ]
    # Alice isn't offered her own routes.
    assert user_client.get("/api/shared-users").json() == []


def test_viewer_gets_routes_without_private_fields(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    _seed_route("alice", "web-1")
    user_client.put("/api/ratings/web-1", json={"fun": 4})
    _share(user_client, True)

    resp = second_user_client.get(_shared_routes_path("alice"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    [feature] = body["features"]
    props = feature["properties"]
    assert props["id"] == "r0"
    assert props["name"] == "alice's route"
    assert props["sport"] == ["hiking"]
    assert props["status"] == "planned"
    assert props["tags"] == ["alps"]
    assert props["notes"] == ""
    assert props["strava_id"] is None
    assert "the key is under the mat" not in resp.text
    assert feature["geometry"] == user_client.get("/api/routes").json()[
        "features"
    ][0]["geometry"]
    # The owner's own (cached) data is untouched.
    own = user_client.get("/api/routes").json()["features"][0]["properties"]
    assert own["notes"] == "the key is under the mat"
    assert own["strava_id"] == 12345
    # Bob's own map is still only his.
    assert second_user_client.get("/api/routes").json()["features"] == []


def test_shared_ids_hide_garmin_activity_ids(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    _seed_route("alice", "garmin-987654321")
    _share(user_client, True)
    resp = second_user_client.get(_shared_routes_path("alice"))
    assert resp.status_code == 200, resp.text
    [feature] = resp.json()["features"]
    assert feature["properties"]["id"] == "r0"
    assert "987654321" not in resp.text
    # Alice still addresses it by its real id.
    own = user_client.get("/api/routes").json()["features"][0]["properties"]
    assert own["id"] == "garmin-987654321"


def test_opting_out_takes_effect_at_once(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    _seed_route("alice", "web-1")
    _share(user_client, True)
    assert second_user_client.get(_shared_routes_path("alice")).status_code == 200
    _share(user_client, False)
    assert second_user_client.get(_shared_routes_path("alice")).status_code == 404
    assert second_user_client.get("/api/shared-users").json() == []


def test_non_sharing_unknown_and_own_ids_are_404(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    _seed_route("alice", "web-1")
    _seed_route("bob", "bob-1")
    _share(second_user_client, True)
    # Alice doesn't share.
    assert second_user_client.get(_shared_routes_path("alice")).status_code == 404
    # No such user.
    assert second_user_client.get("/api/shared/9999/routes").status_code == 404
    # Bob shares, but not with himself.
    assert second_user_client.get(_shared_routes_path("bob")).status_code == 404
    assert user_client.get(_shared_routes_path("bob")).status_code == 200


def test_anonymous_gets_401(user_client: TestClient, anon: TestClient) -> None:
    _seed_route("alice", "web-1")
    _share(user_client, True)
    assert anon.get("/api/shared-users").status_code == 401
    assert anon.get(_shared_routes_path("alice")).status_code == 401


def test_owner_edits_show_up_for_the_viewer(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    _seed_route("alice", "web-1")
    _share(user_client, True)
    path = _shared_routes_path("alice")
    assert second_user_client.get(path).json()["features"][0]["properties"][
        "name"
    ] == "alice's route"  # now warm
    resp = user_client.patch(
        "/api/routes/web-1", json={"name": "Renamed", "notes": "still private"}
    )
    assert resp.status_code == 200, resp.text
    props = second_user_client.get(path).json()["features"][0]["properties"]
    assert props["name"] == "Renamed"
    assert props["notes"] == ""
    user_client.delete("/api/routes/web-1")
    assert second_user_client.get(path).json()["features"] == []


def test_public_link_is_unaffected(
    user_client: TestClient, second_user_client: TestClient, anon: TestClient
) -> None:
    _seed_route("alice", "alice-route")
    _seed_route("bob", "bob-route")
    _share(second_user_client, True)
    _share(user_client, True)
    token = public_links.rotate(ctx().db_path, user_id("alice"))
    base = f"/u/{token}/api"
    # Only alice's routes, with her notes as the link always served them…
    [feature] = anon.get(f"{base}/routes").json()["features"]
    assert feature["properties"]["id"] == "alice-route"
    assert feature["properties"]["notes"] == "the key is under the mat"
    # …and no way to reach anyone's shared routes through the link.
    for path in ("shared-users", f"shared/{user_id('bob')}/routes", "sharing"):
        assert anon.get(f"{base}/{path}").status_code == 404
    # The page is baked read-only, which hides the overlay's panel group.
    assert 'class="readonly"' in anon.get(f"/u/{token}").text


# ── Frontend wiring ──────────────────────────────────────────────────


def test_index_ships_sharing_ui(client: TestClient) -> None:
    html = client.get("/").text
    for element in (
        'id="settings-share-enabled"',
        'id="settings-share-status"',
        'id="shared-group"',
        'id="shared-list"',
    ):
        assert element in html
    # The overlay of other users' routes never shows in the read-only views.
    assert "body.readonly #shared-group" in html
