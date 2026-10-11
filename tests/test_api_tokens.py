"""Personal API tokens: the store (`api_tokens`) and the HTTP side.

Run on the shared fixtures (conftest.py): `user_client` is alice and
`second_user_client` is bob, both signed in on a tmp_path install. A
script is played by `bare`, a cookie-less client that only sends an
`Authorization: Bearer` header.
"""

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import MINIMAL_GPX, ctx, user_id
from wishmap import api_tokens, users
from wishmap.app import app

PIN = {"name": "Hut", "lat": 46.9, "lon": 9.7, "sport": ["hiking"], "status": "idea"}


@pytest.fixture
def bare(client: TestClient) -> Iterator[TestClient]:
    c = TestClient(app)
    yield c
    c.close()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create(c: TestClient, scope: str = "write", **extra: object) -> dict:
    resp = c.post("/api/tokens", json={"name": "script", "scope": scope, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Store ─────────────────────────────────────────────────────────────


def _user(db_path: Path, name: str = "alice") -> int:
    return users.create_user(db_path, name, "correct horse battery").id


def test_create_resolve_list_revoke(db_path: Path) -> None:
    uid = _user(db_path)
    secret, token = api_tokens.create(db_path, uid, "laptop", "read")
    assert secret.startswith(api_tokens.TOKEN_PREFIX)
    auth = api_tokens.resolve(db_path, secret)
    assert auth is not None
    assert (auth.user.id, auth.scope, auth.token_id) == (uid, "read", token.id)
    [listed] = api_tokens.list_tokens(db_path, uid)
    assert listed.name == "laptop"
    assert listed.last_used_at is not None  # the resolve touched it
    assert api_tokens.revoke(db_path, uid, token.id)
    assert api_tokens.resolve(db_path, secret) is None
    assert not api_tokens.revoke(db_path, uid, token.id)


def test_only_the_hash_is_stored(db_path: Path) -> None:
    uid = _user(db_path)
    secret, _ = api_tokens.create(db_path, uid, "x", "write")
    assert secret.encode() not in db_path.read_bytes()


def test_expired_token_does_not_resolve(db_path: Path) -> None:
    uid = _user(db_path)
    secret, _ = api_tokens.create(db_path, uid, "x", "read", timedelta(seconds=-1))
    assert api_tokens.resolve(db_path, secret) is None
    # Still listed, so the owner sees it expired.
    assert len(api_tokens.list_tokens(db_path, uid)) == 1


def test_junk_does_not_resolve(db_path: Path) -> None:
    _user(db_path)
    for junk in ("", "wm_", "nope", "wm_" + "a" * 200):
        assert api_tokens.resolve(db_path, junk) is None


def test_revoke_only_own_tokens(db_path: Path) -> None:
    alice, bob = _user(db_path), _user(db_path, "bob")
    secret, token = api_tokens.create(db_path, alice, "x", "read")
    assert not api_tokens.revoke(db_path, bob, token.id)
    assert api_tokens.resolve(db_path, secret) is not None


def test_token_limit(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_tokens, "MAX_TOKENS_PER_USER", 2)
    uid = _user(db_path)
    api_tokens.create(db_path, uid, "a", "read")
    api_tokens.create(db_path, uid, "b", "read")
    with pytest.raises(api_tokens.TooManyTokensError):
        api_tokens.create(db_path, uid, "c", "read")


# ── Managing tokens over HTTP ─────────────────────────────────────────


def test_create_shows_secret_once(user_client: TestClient) -> None:
    created = _create(user_client, "read", expires_days=30)
    assert created["token"].startswith("wm_")
    assert created["scope"] == "read"
    assert created["expires_at"] is not None
    listed = user_client.get("/api/tokens").json()
    assert [t["id"] for t in listed] == [created["id"]]
    assert "token" not in listed[0]


def test_tokens_are_per_user(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    created = _create(user_client)
    assert second_user_client.get("/api/tokens").json() == []
    assert second_user_client.delete(f"/api/tokens/{created['id']}").status_code == 404
    assert user_client.delete(f"/api/tokens/{created['id']}").status_code == 204
    assert user_client.get("/api/tokens").json() == []


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "scope": "read"},
        {"name": "   ", "scope": "read"},
        {"name": "x", "scope": "admin"},
        {"name": "x", "scope": "read", "expires_days": 0},
    ],
)
def test_create_validates(user_client: TestClient, body: dict) -> None:
    assert user_client.post("/api/tokens", json=body).status_code == 422


def test_token_management_needs_a_session(user_client: TestClient, bare: TestClient) -> None:
    secret = _create(user_client)["token"]
    assert bare.get("/api/tokens").status_code == 401
    # A token can neither list nor mint tokens, nor reach the account.
    for method, path, body in (
        ("GET", "/api/tokens", None),
        ("POST", "/api/tokens", {"name": "x", "scope": "write"}),
        ("GET", "/api/me", None),
        ("GET", "/api/integrations/garmin", None),
        ("POST", "/api/sync", None),
        ("POST", "/api/public-link", None),
        ("PUT", "/api/sharing", {"enabled": True}),
        ("POST", "/api/auth/password", {"current_password": "x", "new_password": "y" * 8}),
    ):
        resp = bare.request(method, path, json=body, headers=_bearer(secret))
        assert resp.status_code == 403, (method, path, resp.text)
        assert resp.json()["detail"] == "session_required"


def test_management_is_csrf_protected(user_client: TestClient) -> None:
    resp = user_client.post(
        "/api/tokens",
        json={"name": "x", "scope": "write"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403


# ── Using a token ─────────────────────────────────────────────────────


def test_read_token_reads_but_cannot_write(user_client: TestClient, bare: TestClient) -> None:
    user_client.post("/api/pins", json=PIN)
    secret = _create(user_client, "read")["token"]
    pins = bare.get("/api/pins", headers=_bearer(secret))
    assert pins.status_code == 200
    assert [f["properties"]["name"] for f in pins.json()["features"]] == ["Hut"]
    assert bare.get("/api/routes", headers=_bearer(secret)).status_code == 200
    assert bare.get("/api/ratings", headers=_bearer(secret)).status_code == 200
    resp = bare.post("/api/pins", json=PIN, headers=_bearer(secret))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "insufficient_scope"


def test_write_token_adds_pins_and_routes(user_client: TestClient, bare: TestClient) -> None:
    # Warm alice's cache first: the token's writes must land in it.
    user_client.get("/api/routes")
    secret = _create(user_client, "write")["token"]
    pin = bare.post("/api/pins", json=PIN, headers=_bearer(secret))
    assert pin.status_code == 201, pin.text
    route = bare.post(
        "/api/routes",
        files={"gpx_file": ("t.gpx", MINIMAL_GPX, "application/gpx+xml")},
        data={"name": "Scripted", "sport": "hiking", "status": "idea"},
        headers=_bearer(secret),
    )
    assert route.status_code == 201, route.text
    route_id = route.json()["route"]["properties"]["id"]
    names = [
        f["properties"]["name"]
        for f in user_client.get("/api/routes").json()["features"]
        if f["properties"]["kind"] == "route"
    ]
    assert names == ["Scripted"]
    assert (
        ctx().user_dir(user_id("alice")) / "gpx" / "web" / f"{route_id}.gpx"
    ).exists()
    patched = bare.patch(
        f"/api/routes/{route_id}", json={"name": "Renamed"}, headers=_bearer(secret)
    )
    assert patched.status_code == 200
    rating = bare.put(f"/api/ratings/{route_id}", json={"fun": 4}, headers=_bearer(secret))
    assert rating.status_code == 200
    assert bare.delete(f"/api/routes/{route_id}", headers=_bearer(secret)).status_code == 204


def test_token_skips_csrf_check(user_client: TestClient, bare: TestClient) -> None:
    secret = _create(user_client)["token"]
    resp = bare.post(
        "/api/pins",
        json=PIN,
        headers={**_bearer(secret), "Origin": "https://elsewhere.example"},
    )
    assert resp.status_code == 201


def test_token_with_cookie_still_csrf_checked(user_client: TestClient) -> None:
    secret = _create(user_client)["token"]
    resp = user_client.post(
        "/api/pins",
        json=PIN,
        headers={**_bearer(secret), "Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403


def test_token_acts_only_for_its_owner(
    user_client: TestClient, second_user_client: TestClient, bare: TestClient
) -> None:
    second_user_client.post("/api/pins", json=PIN)
    secret = _create(user_client, "read")["token"]
    assert bare.get("/api/pins", headers=_bearer(secret)).json()["features"] == []


def test_token_overrides_cookie(user_client: TestClient) -> None:
    """A request with a token is the token's, whatever cookie it carries."""
    secret = _create(user_client, "read")["token"]
    resp = user_client.post("/api/pins", json=PIN, headers=_bearer(secret))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "insufficient_scope"


def test_bad_revoked_and_expired_tokens(user_client: TestClient, bare: TestClient) -> None:
    resp = bare.get("/api/pins", headers=_bearer("wm_nope"))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_token"
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")

    created = _create(user_client)
    user_client.delete(f"/api/tokens/{created['id']}")
    assert bare.get("/api/pins", headers=_bearer(created["token"])).status_code == 401

    secret, _ = api_tokens.create(
        ctx().db_path, user_id("alice"), "old", "read", timedelta(seconds=-1)
    )
    assert bare.get("/api/pins", headers=_bearer(secret)).status_code == 401


def test_failed_tokens_are_throttled(user_client: TestClient, bare: TestClient) -> None:
    secret = _create(user_client, "read")["token"]
    throttle = ctx().token_throttle
    for _ in range(throttle.max_failures):
        assert bare.get("/api/pins", headers=_bearer("wm_bad")).status_code == 401
    # Locked out: even the valid token waits.
    resp = bare.get("/api/pins", headers=_bearer(secret))
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    # Logins are throttled separately.
    assert ctx().login_throttle.retry_after("alice") == 0


def test_last_used_is_recorded(user_client: TestClient, bare: TestClient) -> None:
    secret = _create(user_client, "read")["token"]
    assert user_client.get("/api/tokens").json()[0]["last_used_at"] is None
    bare.get("/api/pins", headers=_bearer(secret))
    assert user_client.get("/api/tokens").json()[0]["last_used_at"] is not None


def test_deleting_the_user_drops_tokens(user_client: TestClient, bare: TestClient) -> None:
    secret = _create(user_client)["token"]
    conn = sqlite3.connect(ctx().db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM users WHERE id = ?", (user_id("alice"),))
    conn.commit()
    conn.close()
    assert bare.get("/api/pins", headers=_bearer(secret)).status_code == 401
