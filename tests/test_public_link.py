"""HTTP tests for public read-only links: /api/public-link and /u/<token>.

Run on the shared fixtures (conftest.py): `user_client` is alice and
`second_user_client` is bob, both signed in on a tmp_path install;
`anon` is a further cookie jar with no session at all, standing in for a
stranger who was handed the link.
"""

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.helpers import MINIMAL_GPX, ctx, user_id
from wishmap import export, public_links, ratings, routes_store
from wishmap.app import STATIC_DIR, app
from wishmap.models import RouteConfig

DATA = ("config", "pins", "routes", "ratings")


@pytest.fixture
def anon(client: TestClient) -> Iterator[TestClient]:
    c = TestClient(app)
    yield c
    c.close()


def _seed_route(username: str, route_id: str) -> None:
    """Give a user a renderable route (DB row + GPX in their dir)."""
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
            gpx=rel,
        ),
    )


def _publish(c: TestClient) -> str:
    """Create (or rotate) the signed-in user's link; return its token."""
    resp = c.post("/api/public-link")
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    match = re.fullmatch(r"http://testserver/u/([A-Za-z0-9_-]{43})", url)
    assert match, url
    return match.group(1)


def _ids(c: TestClient, path: str) -> set[str]:
    return {f["properties"]["id"] for f in c.get(path).json()["features"]}


# ── /api/public-link: create, rotate, revoke ─────────────────────────


def test_no_link_by_default(user_client: TestClient) -> None:
    resp = user_client.get("/api/public-link")
    assert resp.status_code == 200
    assert resp.json() == {"url": None}


def test_create_returns_absolute_url(user_client: TestClient) -> None:
    token = _publish(user_client)
    url = f"http://testserver/u/{token}"
    assert user_client.get("/api/public-link").json() == {"url": url}
    assert user_client.get("/api/me").json()["public_link"] == url
    assert public_links.get_token(ctx().db_path, user_id("alice")) == token


def test_url_follows_the_requests_host(user_client: TestClient) -> None:
    # Built from request.base_url, so it names the host the user reached
    # (behind a proxy: what --proxy-headers reports).
    resp = user_client.post("/api/public-link", headers={"host": "maps.example:8443"})
    assert resp.json()["url"].startswith("http://maps.example:8443/u/")


def test_rotate_replaces_the_token(user_client: TestClient, anon: TestClient) -> None:
    old = _publish(user_client)
    new = _publish(user_client)
    assert new != old
    assert anon.get(f"/u/{old}").status_code == 404
    assert anon.get(f"/u/{old}/api/routes").status_code == 404
    assert anon.get(f"/u/{new}").status_code == 200


def test_revoke_takes_the_link_down(user_client: TestClient, anon: TestClient) -> None:
    token = _publish(user_client)
    assert anon.get(f"/u/{token}/api/pins").status_code == 200
    resp = user_client.delete("/api/public-link")
    assert resp.status_code == 204
    assert user_client.get("/api/public-link").json() == {"url": None}
    assert user_client.get("/api/me").json()["public_link"] is None
    assert anon.get(f"/u/{token}").status_code == 404
    for name in DATA:
        assert anon.get(f"/u/{token}/api/{name}").status_code == 404
    # Idempotent.
    assert user_client.delete("/api/public-link").status_code == 204


def test_links_are_per_user(
    user_client: TestClient, second_user_client: TestClient, anon: TestClient
) -> None:
    alice = _publish(user_client)
    bob = _publish(second_user_client)
    assert alice != bob
    # Bob revoking (or rotating) his link leaves alice's alone.
    _publish(second_user_client)
    second_user_client.delete("/api/public-link")
    assert anon.get(f"/u/{alice}").status_code == 200
    assert anon.get(f"/u/{bob}").status_code == 404
    assert user_client.get("/api/public-link").json()["url"].endswith(alice)


# ── /u/<token>: the read-only page ───────────────────────────────────


def test_page_is_the_app_baked_read_only(user_client: TestClient, anon: TestClient) -> None:
    token = _publish(user_client)
    resp = anon.get(f"/u/{token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    html = resp.text
    # The very transform the static export uses, pointed at the live API.
    assert html == export.transform_html(
        (STATIC_DIR / "index.html").read_text(),
        api_base=f"/u/{token}/api",
        static=False,
    )
    assert re.search(
        rf'(?m)^<body class="readonly" data-api-base="/u/{token}/api">$', html
    )
    assert 'data-api-static="1">' not in html
    assert "fonts.googleapis.com" not in html


def test_page_keeps_the_token_out_of_referers_and_search(
    user_client: TestClient, anon: TestClient
) -> None:
    token = _publish(user_client)
    resp = anon.get(f"/u/{token}")
    assert resp.headers["referrer-policy"] == "strict-origin"
    assert resp.headers["x-robots-tag"] == "noindex"


# ── /u/<token>/api: the owner's data, read-only ──────────────────────


def test_anonymous_reads_the_owners_data(
    user_client: TestClient, anon: TestClient
) -> None:
    _seed_route("alice", "web-1")
    user_client.post(
        "/api/pins",
        json={"name": "Hut", "lat": 46.5, "lon": 9.5, "sport": "hiking", "status": "idea"},
    )
    user_client.put("/api/ratings/web-1", json={"fun": 4})
    token = _publish(user_client)

    config = anon.get(f"/u/{token}/api/config").json()
    assert config["readonly"] is True
    assert config["title"] == "test"
    for name in ("pins", "routes", "ratings"):
        resp = anon.get(f"/u/{token}/api/{name}")
        assert resp.status_code == 200
        assert resp.json() == user_client.get(f"/api/{name}").json()
    assert anon.get(f"/u/{token}/api/ratings").json()["web-1"]["fun"] == 4
    # The anonymous client never got a session out of it.
    assert anon.get("/api/me").status_code == 401


def test_owner_edits_show_up_live(user_client: TestClient, anon: TestClient) -> None:
    token = _publish(user_client)
    assert anon.get(f"/u/{token}/api/pins").json()["features"] == []  # now warm
    pin = user_client.post(
        "/api/pins",
        json={"name": "Hut", "lat": 46.5, "lon": 9.5, "sport": "hiking", "status": "idea"},
    ).json()
    assert _ids(anon, f"/u/{token}/api/pins") == {pin["properties"]["id"]}
    user_client.delete(f"/api/pins/{pin['properties']['id']}")
    assert _ids(anon, f"/u/{token}/api/pins") == set()


def test_link_shows_only_its_owner(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    _seed_route("alice", "alice-route")
    _seed_route("bob", "bob-route")
    ratings.upsert(ctx().db_path, user_id("bob"), "bob-route", {"fun": 2})
    alice = _publish(user_client)
    # Whoever visits — here bob, signed in — sees the link owner's map.
    assert _ids(second_user_client, f"/u/{alice}/api/routes") == {"alice-route"}
    assert _ids(second_user_client, f"/u/{alice}/api/pins") == {"alice-route-start"}
    assert second_user_client.get(f"/u/{alice}/api/ratings").json() == {}
    # …and their own session still reads their own.
    assert _ids(second_user_client, "/api/routes") == {"bob-route"}


def test_link_is_read_only(user_client: TestClient, anon: TestClient) -> None:
    _seed_route("alice", "web-1")
    token = _publish(user_client)
    base = f"/u/{token}/api"
    # Nothing under the link accepts a write…
    assert anon.put(f"{base}/ratings/web-1", json={"fun": 1}).status_code in (404, 405)
    assert anon.post(f"{base}/pins", json={}).status_code in (404, 405)
    assert anon.delete(f"{base}/routes/web-1").status_code in (404, 405)
    assert anon.patch(f"{base}/routes/web-1", json={"name": "x"}).status_code in (404, 405)
    # …nor serves anything but the four read endpoints.
    for path in ("me", "sync/status", "integrations/garmin", "public-link"):
        assert anon.get(f"{base}/{path}").status_code == 404
    # Even alice, signed in, can't write through her link.
    assert user_client.put(f"{base}/ratings/web-1", json={"fun": 1}).status_code in (404, 405)
    assert ratings.get_all(ctx().db_path, user_id("alice")) == {}


@pytest.mark.parametrize(
    "path",
    ["", "/api/config", "/api/pins", "/api/routes", "/api/ratings"],
)
def test_unknown_token_is_an_ordinary_404(
    user_client: TestClient, anon: TestClient, path: str
) -> None:
    _publish(user_client)  # a link exists, just not this one
    resp = anon.get(f"/u/{'x' * 43}{path}")
    assert resp.status_code == 404
    # Exactly what any missing page answers: no hint of a user or a link.
    missing = anon.get("/no/such/page")
    assert resp.json() == missing.json() == {"detail": "Not Found"}
    assert "referrer-policy" not in resp.headers


def test_link_survives_a_restart(user_client: TestClient) -> None:
    _seed_route("alice", "web-1")
    token = _publish(user_client)
    # A fresh lifespan: cold state, the token resolved from the DB again.
    with TestClient(app) as fresh:
        assert _ids(fresh, f"/u/{token}/api/routes") == {"web-1"}


# ── Frontend wiring ──────────────────────────────────────────────────


def test_index_ships_edit_pin_and_link_ui(client: TestClient) -> None:
    html = client.get("/").text
    for element in (
        'id="addpin-button"',
        'id="pin-overlay"',
        'id="place-hint"',
        'id="ar-notes"',
        'id="link-create"',
        'id="link-rotate"',
        'id="link-revoke"',
        'id="link-copy"',
    ):
        assert element in html
    # The read-only gate hides every new edit affordance.
    for selector in (".place-hint", ".popup-actions", ".popup-edit", ".addroute-overlay"):
        assert f"body.readonly {selector}" in html
    # An inline icon: no /favicon.ico request (a 404 on every page load).
    assert '<link rel="icon" href="data:' in html
