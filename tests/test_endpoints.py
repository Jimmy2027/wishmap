"""HTTP integration tests for the ratings endpoints and the index page.

Run against the shared fixtures (conftest.py): a tmp_path install with a
signed-in `user_client` (alice) and, for isolation checks, a
`second_user_client` (bob) on the same running app. Each route the tests
rate is seeded straight into alice's account (DB row + GPX in her dir).
"""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import MINIMAL_GPX, ctx, user_id
from wishmap import routes_store
from wishmap.models import RouteConfig


def _seed_route(username: str, route_id: str = "r1") -> None:
    uid = user_id(username)
    rel = f"gpx/import/{route_id}.gpx"
    path = ctx().user_dir(uid) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MINIMAL_GPX)
    routes_store.insert(
        ctx().db_path,
        uid,
        RouteConfig(
            id=route_id,
            name="Route One",
            sport=["hiking"],
            status="planned",  # type: ignore[arg-type]
            gpx=rel,
        ),
        source="import",
    )


@pytest.fixture
def client_with_route(user_client: TestClient) -> TestClient:
    _seed_route("alice")
    return user_client


def test_config_readonly_defaults_false(client: TestClient) -> None:
    # REGRESSION (IRON RULE): the live server must always report readonly=False.
    # Only the static `wishmap export` artifact carries readonly=True. A flip
    # here would silently hide the editing controls on the local app.
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "test"
    assert body["readonly"] is False


def test_get_ratings_empty(user_client: TestClient) -> None:
    resp = user_client.get("/api/ratings")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_put_unknown_route_id_is_404(user_client: TestClient) -> None:
    resp = user_client.put("/api/ratings/nope", json={"fun": 3})
    assert resp.status_code == 404


def test_put_known_route_returns_rating(client_with_route: TestClient) -> None:
    resp = client_with_route.put("/api/ratings/r1", json={"fun": 4})
    assert resp.status_code == 200
    body = resp.json()
    assert body["fun"] == 4
    assert body["difficulty"] is None
    assert body["scenery"] is None
    assert body["updated_at"]


def test_get_after_put(client_with_route: TestClient) -> None:
    client_with_route.put("/api/ratings/r1", json={"fun": 4, "scenery": 5})
    resp = client_with_route.get("/api/ratings")
    assert resp.status_code == 200
    body = resp.json()
    assert "r1" in body
    assert body["r1"]["fun"] == 4
    assert body["r1"]["scenery"] == 5
    assert body["r1"]["difficulty"] is None


def test_put_null_clears_axis(client_with_route: TestClient) -> None:
    client_with_route.put("/api/ratings/r1", json={"fun": 4, "scenery": 5})
    resp = client_with_route.put("/api/ratings/r1", json={"fun": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["fun"] is None
    assert body["scenery"] == 5


def test_put_absent_axis_preserved(client_with_route: TestClient) -> None:
    client_with_route.put("/api/ratings/r1", json={"fun": 4, "scenery": 5})
    resp = client_with_route.put("/api/ratings/r1", json={"difficulty": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["fun"] == 4
    assert body["difficulty"] == 2
    assert body["scenery"] == 5


def test_put_empty_body_refreshes_updated_at(client_with_route: TestClient) -> None:
    first = client_with_route.put("/api/ratings/r1", json={"fun": 4}).json()
    second = client_with_route.put("/api/ratings/r1", json={}).json()
    assert second["fun"] == 4
    assert second["updated_at"] >= first["updated_at"]


@pytest.mark.parametrize("bad", [0, 6, -1, 100])
def test_put_out_of_range_is_422(client_with_route: TestClient, bad: int) -> None:
    resp = client_with_route.put("/api/ratings/r1", json={"fun": bad})
    assert resp.status_code == 422


# ── Cross-user isolation ─────────────────────────────────────────────


def test_ratings_are_per_user(
    client_with_route: TestClient, second_user_client: TestClient
) -> None:
    client_with_route.put("/api/ratings/r1", json={"fun": 5})
    # Bob doesn't own r1: he can neither see alice's rating nor rate it.
    assert second_user_client.get("/api/ratings").json() == {}
    assert second_user_client.put("/api/ratings/r1", json={"fun": 1}).status_code == 404

    # Same route id in bob's own account: independent ratings.
    _seed_route("bob")
    assert second_user_client.put("/api/ratings/r1", json={"fun": 1}).status_code == 200
    assert client_with_route.get("/api/ratings").json()["r1"]["fun"] == 5
    assert second_user_client.get("/api/ratings").json()["r1"]["fun"] == 1


# ── Index page ───────────────────────────────────────────────────────


def test_gpg_unlock_endpoint_is_gone(user_client: TestClient) -> None:
    # The GPG/pass unlock flow was removed; nothing serves /api/unlock.
    resp = user_client.post("/api/unlock", json={"passphrase": "x"})
    assert resp.status_code == 404


def test_index_has_no_gpg_unlock_ui(client: TestClient) -> None:
    html = client.get("/").text
    for needle in ("/api/unlock", "unlock-overlay", "needs_passphrase", "GPG"):
        assert needle not in html


def test_large_responses_are_gzipped(client: TestClient) -> None:
    # /api/routes runs to megabytes; the middleware compresses anything big.
    resp = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    small = client.get("/api/config", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers
