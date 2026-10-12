"""HTTP tests for accounts: /api/auth/*, /api/me, /api/config, the session
and vault cookies, the same-origin (CSRF) guard on every mutation, and the
login throttle.
"""

import hashlib
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import MINIMAL_GPX, PASSWORD, ctx, user_id
from wishmap import (
    db,
    integrations_store,
    pins_store,
    public_links,
    shared_maps,
    users,
    vault,
)
from wishmap.app import app
from wishmap.auth import SESSION_COOKIE, VAULT_COOKIE
from wishmap.state import LoginThrottle


def _register(
    c: TestClient, username: str = "alice", password: str = PASSWORD
) -> httpx.Response:
    return c.post("/api/auth/register", json={"username": username, "password": password})


def _login(
    c: TestClient, username: str = "alice", password: str = PASSWORD
) -> httpx.Response:
    return c.post("/api/auth/login", json={"username": username, "password": password})


def _set_cookie(resp: httpx.Response, name: str = SESSION_COOKIE) -> str:
    """The response's Set-Cookie header for cookie `name`."""
    headers = [h for h in resp.headers.get_list("set-cookie") if h.startswith(f"{name}=")]
    assert len(headers) == 1, f"no single Set-Cookie for {name}"
    return headers[0]


# ── /api/config and /api/me ──────────────────────────────────────────


def test_config_is_public_and_reports_registration(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert body == {"title": "test", "readonly": False, "registration_open": True}


def test_me_requires_login(client: TestClient) -> None:
    resp = client.get("/api/me")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "not_authenticated"


def test_me_reports_account(user_client: TestClient) -> None:
    body = user_client.get("/api/me").json()
    assert body == {
        "username": "alice",
        "public_link": None,
        "integrations": {},
        "vault_unlocked": True,
        "sharing": False,
        "is_admin": False,
    }


def test_me_reports_public_link_and_integrations(user_client: TestClient) -> None:
    uid = user_id("alice")
    token = public_links.rotate(ctx().db_path, uid)
    integrations_store.upsert_settings(
        ctx().db_path, uid, "garmin", username="a@example.com", settings={}
    )
    integrations_store.upsert_settings(
        ctx().db_path, uid, "strava", username=None, settings={}
    )
    integrations_store.set_tokens(ctx().db_path, uid, "strava", b"sealed")
    body = user_client.get("/api/me").json()
    assert body["public_link"] == f"http://testserver/u/{token}"
    # A summary only: never the (sealed) tokens themselves.
    assert body["integrations"] == {
        "garmin": {"username": "a@example.com", "logged_in": False},
        "strava": {"username": None, "logged_in": True},
    }


# ── Register ─────────────────────────────────────────────────────────


def test_register_signs_in(client: TestClient) -> None:
    resp = _register(client)
    assert resp.status_code == 201
    assert resp.json() == {"username": "alice"}
    assert client.get("/api/me").json()["username"] == "alice"


def test_register_duplicate_is_409_case_insensitively(client: TestClient) -> None:
    assert _register(client, "Alice").status_code == 201
    other = TestClient(app)
    resp = _register(other, "alice")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "username_taken"


@pytest.mark.parametrize(
    ("username", "password", "needle"),
    [
        ("a b", PASSWORD, "Usernames are"),
        ("ab", PASSWORD, "Usernames are"),
        ("alice", "short", "at least"),
    ],
)
def test_register_rejects_bad_credentials(
    client: TestClient, username: str, password: str, needle: str
) -> None:
    resp = _register(client, username, password)
    assert resp.status_code == 422
    assert needle in resp.json()["detail"]
    assert SESSION_COOKIE not in client.cookies


@pytest.mark.parametrize("wishmap_toml", ['allow_registration = false\n'])
def test_register_closed(client: TestClient) -> None:
    assert client.get("/api/config").json()["registration_open"] is False
    resp = _register(client)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "registration_closed"
    assert users.get_by_username(ctx().db_path, "alice") is None


@pytest.mark.parametrize("wishmap_toml", ['allow_registration = false\n'])
def test_existing_accounts_can_still_log_in_when_registration_closed(
    client: TestClient,
) -> None:
    users.create_user(ctx().db_path, "alice", PASSWORD)
    assert _login(client).status_code == 200


# ── Session cookie ───────────────────────────────────────────────────


def test_session_cookie_flags(client: TestClient) -> None:
    header = _set_cookie(_register(client))
    assert header.startswith(f"{SESSION_COOKIE}=")
    attrs = [a.strip().lower() for a in header.split(";")]
    assert "httponly" in attrs
    assert "samesite=lax" in attrs
    assert "path=/" in attrs
    assert f"max-age={30 * 24 * 3600}" in attrs
    assert "secure" not in attrs  # secure_cookies defaults off (plain-HTTP dev)


@pytest.mark.parametrize(
    "wishmap_toml", ["secure_cookies = true\nsession_days = 7\n"]
)
def test_session_cookie_secure_and_lifetime_from_settings(client: TestClient) -> None:
    attrs = [a.strip().lower() for a in _set_cookie(_register(client)).split(";")]
    assert "secure" in attrs
    assert f"max-age={7 * 24 * 3600}" in attrs


def test_cookie_holds_token_but_db_only_its_hash(client: TestClient) -> None:
    _register(client)
    token = client.cookies[SESSION_COOKIE]
    with db.connect(ctx().db_path) as conn:
        hashes = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
    assert hashes == [hashlib.sha256(token.encode()).hexdigest()]


def test_bogus_cookie_is_401(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "forged-token")
    assert client.get("/api/me").status_code == 401


def test_expired_session_is_401(client: TestClient) -> None:
    alice = users.create_user(ctx().db_path, "alice", PASSWORD)
    token = users.create_session(ctx().db_path, alice.id, timedelta(seconds=-1))
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/api/me").status_code == 401


# ── Login / logout ───────────────────────────────────────────────────


def test_login_and_logout(client: TestClient) -> None:
    users.create_user(ctx().db_path, "alice", PASSWORD)
    resp = _login(client, "ALICE")
    assert resp.status_code == 200
    assert resp.json() == {"username": "alice"}
    token = client.cookies[SESSION_COOKIE]
    assert client.get("/api/me").status_code == 200

    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    assert "max-age=0" in _set_cookie(resp).lower()
    # The vault key goes with the session.
    assert "max-age=0" in _set_cookie(resp, VAULT_COOKIE).lower()
    assert VAULT_COOKIE not in client.cookies
    assert client.get("/api/me").status_code == 401
    # The session is revoked server-side, not just forgotten by the browser.
    assert users.resolve_session(ctx().db_path, token) is None


def test_logout_without_session_is_fine(client: TestClient) -> None:
    assert client.post("/api/auth/logout").status_code == 204


@pytest.mark.parametrize(
    ("username", "password"), [("alice", "wrong password"), ("nobody", PASSWORD)]
)
def test_login_failure_is_401_without_cookie(
    client: TestClient, username: str, password: str
) -> None:
    users.create_user(ctx().db_path, "alice", PASSWORD)
    resp = _login(client, username, password)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_credentials"
    assert "set-cookie" not in resp.headers


def test_login_replaces_previous_session(user_client: TestClient) -> None:
    old = user_client.cookies[SESSION_COOKIE]
    assert _login(user_client).status_code == 200
    new = user_client.cookies[SESSION_COOKIE]
    assert new != old
    assert users.resolve_session(ctx().db_path, old) is None


def test_sessions_are_per_browser(user_client: TestClient) -> None:
    laptop = TestClient(app)
    assert _login(laptop).status_code == 200
    assert laptop.post("/api/auth/logout").status_code == 204
    # Logging out on one device leaves the other signed in.
    assert user_client.get("/api/me").status_code == 200


# ── Vault cookie ─────────────────────────────────────────────────────


def test_vault_cookie_flags(client: TestClient) -> None:
    header = _set_cookie(_register(client), VAULT_COOKIE)
    attrs = [a.strip().lower() for a in header.split(";")]
    assert "httponly" in attrs
    # Strict, unlike the session's Lax: no cross-site request carries it.
    assert "samesite=strict" in attrs
    assert "path=/" in attrs
    assert f"max-age={30 * 24 * 3600}" in attrs
    assert "secure" not in attrs


@pytest.mark.parametrize("wishmap_toml", ["secure_cookies = true\n"])
def test_vault_cookie_secure_from_settings(client: TestClient) -> None:
    header = _set_cookie(_register(client), VAULT_COOKIE)
    assert "secure" in [a.strip().lower() for a in header.split(";")]


def test_every_login_recovers_the_same_vault_key(client: TestClient) -> None:
    _register(client)
    key = client.cookies[VAULT_COOKIE]
    laptop = TestClient(app)
    assert _login(laptop, "ALICE").status_code == 200
    assert laptop.cookies[VAULT_COOKIE] == key
    # The key is never stored in the clear.
    raw = vault.decode_key(key)
    assert raw is not None
    for path in ctx().db_path.parent.glob("wishmap.db*"):
        assert raw not in path.read_bytes()


def test_failed_login_sets_no_vault_cookie(client: TestClient) -> None:
    users.create_user(ctx().db_path, "alice", PASSWORD)
    resp = _login(client, password="wrong password")
    assert resp.status_code == 401
    assert VAULT_COOKIE not in client.cookies


def test_me_reports_a_missing_or_stale_vault_key(user_client: TestClient) -> None:
    good = user_client.cookies[VAULT_COOKIE]
    user_client.cookies.delete(VAULT_COOKIE)
    assert user_client.get("/api/me").json()["vault_unlocked"] is False
    # Well-formed, but not this account's key.
    user_client.cookies.set(VAULT_COOKIE, vault.encode_key(vault.new_key()))
    assert user_client.get("/api/me").json()["vault_unlocked"] is False
    user_client.cookies.set(VAULT_COOKIE, "junk")
    assert user_client.get("/api/me").json()["vault_unlocked"] is False
    user_client.cookies.set(VAULT_COOKIE, good)
    assert user_client.get("/api/me").json()["vault_unlocked"] is True


def test_another_users_vault_key_is_stale(
    user_client: TestClient, second_user_client: TestClient
) -> None:
    user_client.cookies.set(VAULT_COOKIE, second_user_client.cookies[VAULT_COOKIE])
    assert user_client.get("/api/me").json()["vault_unlocked"] is False


def test_password_change_keeps_the_vault_key(user_client: TestClient) -> None:
    key = user_client.cookies[VAULT_COOKIE]
    resp = _change(user_client, PASSWORD, "new password!")
    assert resp.status_code == 204
    # Re-set (e.g. for a browser that lacked it), but the same key.
    assert _set_cookie(resp, VAULT_COOKIE).startswith(f"{VAULT_COOKIE}={key};")
    other = TestClient(app)
    assert _login(other, password="new password!").status_code == 200
    assert other.cookies[VAULT_COOKIE] == key


def test_password_change_unlocks_a_browser_without_the_vault_cookie(
    user_client: TestClient,
) -> None:
    key = user_client.cookies[VAULT_COOKIE]
    user_client.cookies.delete(VAULT_COOKIE)
    assert _change(user_client, PASSWORD, "new password!").status_code == 204
    assert user_client.cookies[VAULT_COOKIE] == key
    assert user_client.get("/api/me").json()["vault_unlocked"] is True


def test_account_without_vault_gets_one_at_login(client: TestClient) -> None:
    """Accounts created before vaults existed (vault columns NULL) are
    upgraded at their next login, and keep that vault afterwards."""
    alice = users.create_user(ctx().db_path, "alice", PASSWORD)
    with db.connect(ctx().db_path) as conn:
        conn.execute(
            "UPDATE users SET vault_salt = NULL, vault_wrapped = NULL WHERE id = ?",
            (alice.id,),
        )
    # A session from those days: signed in, but no vault cookie.
    client.cookies.set(
        SESSION_COOKIE,
        users.create_session(ctx().db_path, alice.id, timedelta(days=1)),
    )
    assert client.get("/api/me").json()["vault_unlocked"] is False
    assert client.post("/api/sync").status_code == 409

    assert _login(client).status_code == 200
    key = client.cookies[VAULT_COOKIE]
    assert client.get("/api/me").json()["vault_unlocked"] is True
    other = TestClient(app)
    assert _login(other).status_code == 200
    assert other.cookies[VAULT_COOKIE] == key


# ── Login throttle ───────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(client: TestClient) -> _Clock:
    c = _Clock()
    ctx().login_throttle.clock = c
    return c


def test_login_throttle_locks_after_five_failures(
    client: TestClient, clock: _Clock
) -> None:
    users.create_user(ctx().db_path, "alice", PASSWORD)
    for _ in range(5):
        assert _login(client, password="wrong password").status_code == 401
    # Locked out — even the right password, and the name's case doesn't help.
    resp = _login(client, "ALICE")
    assert resp.status_code == 429
    assert resp.json()["detail"] == "too_many_attempts"
    assert resp.headers["retry-after"] == "60"
    assert SESSION_COOKIE not in client.cookies

    clock.now += 61
    assert _login(client).status_code == 200


def test_login_throttle_is_per_username(client: TestClient, clock: _Clock) -> None:
    users.create_user(ctx().db_path, "alice", PASSWORD)
    users.create_user(ctx().db_path, "bob", PASSWORD)
    for _ in range(5):
        _login(client, password="wrong password")
    assert _login(client, "alice").status_code == 429
    assert _login(client, "bob").status_code == 200


def test_success_resets_failure_count(client: TestClient, clock: _Clock) -> None:
    users.create_user(ctx().db_path, "alice", PASSWORD)
    for _ in range(4):
        _login(client, password="wrong password")
    assert _login(client).status_code == 200
    for _ in range(4):
        assert _login(client, password="wrong password").status_code == 401
    assert _login(client).status_code == 200


def test_failures_far_apart_do_not_add_up(client: TestClient, clock: _Clock) -> None:
    users.create_user(ctx().db_path, "alice", PASSWORD)
    for _ in range(4):
        _login(client, password="wrong password")
    clock.now += 16 * 60
    assert _login(client, password="wrong password").status_code == 401
    assert _login(client).status_code == 200


def test_unknown_usernames_are_throttled_too(client: TestClient, clock: _Clock) -> None:
    for _ in range(5):
        assert _login(client, "ghost").status_code == 401
    assert _login(client, "ghost").status_code == 429


def test_parallel_logins_get_no_more_guesses_than_the_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A burst of parallel wrong-password logins must not each get a
    # password check in before the first failure is recorded.
    users.create_user(ctx().db_path, "alice", PASSWORD)
    checks = 0
    checks_lock = threading.Lock()
    real_login = users.login

    def slow_login(db_path: Path, username: str, password: str) -> users.Login | None:
        nonlocal checks
        with checks_lock:
            checks += 1
        time.sleep(0.05)  # the window a real scrypt hash leaves open
        return real_login(db_path, username, password)

    monkeypatch.setattr(users, "login", slow_login)

    def attempt(_: int) -> int:
        return _login(TestClient(app), password="wrong password").status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = Counter(pool.map(attempt, range(40)))
    assert checks <= 5
    assert codes[401] == checks
    assert codes[429] == 40 - checks
    assert ctx().login_throttle.retry_after("alice") > 0


def test_failure_recorded_while_locked_keeps_the_lock() -> None:
    throttle = LoginThrottle(max_failures=2)
    throttle.clock = lambda: 1000.0
    # Two checks in flight; a third is refused while they could still fail.
    assert throttle.begin("k") == 0
    assert throttle.begin("k") == 0
    assert throttle.begin("k") > 0
    throttle.record_failure("k")
    throttle.record_failure("k")  # locks
    assert throttle.retry_after("k") == 60
    # A check reserved before the lock that fails afterwards keeps it locked.
    throttle.record_failure("k")
    assert throttle.retry_after("k") == 60
    assert throttle.begin("k") == 60


def test_cancelled_attempt_frees_its_reservation() -> None:
    throttle = LoginThrottle(max_failures=1)
    assert throttle.begin("k") == 0
    assert throttle.begin("k") > 0
    throttle.cancel("k")
    assert throttle.begin("k") == 0


# ── Password change ──────────────────────────────────────────────────


def _change(c: TestClient, current: str, new: str) -> httpx.Response:
    return c.post(
        "/api/auth/password",
        json={"current_password": current, "new_password": new},
    )


def test_change_password_requires_login(client: TestClient) -> None:
    assert _change(client, PASSWORD, "new password!").status_code == 401


def test_change_password_signs_out_other_sessions(user_client: TestClient) -> None:
    other_device = TestClient(app)
    assert _login(other_device).status_code == 200

    assert _change(user_client, PASSWORD, "new password!").status_code == 204
    assert user_client.get("/api/me").status_code == 200  # this session survives
    assert other_device.get("/api/me").status_code == 401
    assert _login(TestClient(app), password=PASSWORD).status_code == 401
    assert _login(TestClient(app), password="new password!").status_code == 200


def test_change_password_wrong_current_is_403(user_client: TestClient) -> None:
    resp = _change(user_client, "not my password", "new password!")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "wrong_password"
    # Not 401: that would tell the web UI the session is gone.
    assert user_client.get("/api/me").status_code == 200


def test_change_password_rejects_weak_new_password(user_client: TestClient) -> None:
    resp = _change(user_client, PASSWORD, "short")
    assert resp.status_code == 422
    assert _login(TestClient(app)).status_code == 200  # unchanged


def test_change_password_is_throttled(
    user_client: TestClient, clock: _Clock
) -> None:
    for _ in range(5):
        assert _change(user_client, "nope nope", "new password!").status_code == 403
    assert _change(user_client, PASSWORD, "new password!").status_code == 429


# ── Same-origin (CSRF) guard on every mutation ───────────────────────


Mutation = Callable[[dict[str, str]], httpx.Response]


def _mutations(c: TestClient) -> dict[str, Mutation]:
    gpx = {"gpx_file": ("r.gpx", MINIMAL_GPX, "application/gpx+xml")}
    form = {"name": "x", "sport": "hiking", "status": "idea"}
    creds = {"username": "carol", "password": PASSWORD}
    pin = {"name": "x", "lat": 1, "lon": 1, "sport": "hiking", "status": "idea"}
    return {
        "register": lambda h: c.post("/api/auth/register", json=creds, headers=h),
        "login": lambda h: c.post("/api/auth/login", json=creds, headers=h),
        "logout": lambda h: c.post("/api/auth/logout", headers=h),
        "password": lambda h: c.post(
            "/api/auth/password",
            json={"current_password": PASSWORD, "new_password": "whatever123"},
            headers=h,
        ),
        "add_route": lambda h: c.post("/api/routes", data=form, files=gpx, headers=h),
        "delete_route": lambda h: c.delete("/api/routes/r1", headers=h),
        "edit_route": lambda h: c.patch("/api/routes/r1", json={"name": "x"}, headers=h),
        "add_pin": lambda h: c.post("/api/pins", json=pin, headers=h),
        "edit_pin": lambda h: c.patch("/api/pins/p1", json={"name": "x"}, headers=h),
        "delete_pin": lambda h: c.delete("/api/pins/p1", headers=h),
        "publish_link": lambda h: c.post("/api/public-link", headers=h),
        "revoke_link": lambda h: c.delete("/api/public-link", headers=h),
        "sharing": lambda h: c.put("/api/sharing", json={"enabled": True}, headers=h),
        "rate": lambda h: c.put("/api/ratings/r1", json={"fun": 3}, headers=h),
        "sync": lambda h: c.post("/api/sync", headers=h),
        "garmin_mfa": lambda h: c.post("/api/garmin/mfa", json={"code": "1"}, headers=h),
        "garmin_settings": lambda h: c.put(
            "/api/integrations/garmin", json={"username": "g@example.com"}, headers=h
        ),
        "garmin_forget": lambda h: c.delete("/api/integrations/garmin", headers=h),
        "strava_archive": lambda h: c.post(
            "/api/integrations/strava/archive",
            files={"archive": ("activities.csv", b"x", "text/csv")},
            headers=h,
        ),
    }


MUTATIONS = [
    "register",
    "login",
    "logout",
    "password",
    "add_route",
    "delete_route",
    "edit_route",
    "add_pin",
    "edit_pin",
    "delete_pin",
    "publish_link",
    "revoke_link",
    "sharing",
    "rate",
    "sync",
    "garmin_mfa",
    "garmin_settings",
    "garmin_forget",
    "strava_archive",
]


@pytest.mark.parametrize("name", MUTATIONS)
@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
        # Browsers without Sec-Fetch-* still send Origin.
        {"origin": "https://evil.example"},
        {"origin": "null"},
    ],
)
def test_cross_origin_mutations_are_403(
    user_client: TestClient, name: str, headers: dict[str, str]
) -> None:
    resp = _mutations(user_client)[name](headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "cross_origin_blocked"
    # Still signed in, nothing changed.
    assert user_client.get("/api/me").status_code == 200
    assert users.get_by_username(ctx().db_path, "carol") is None
    assert integrations_store.get_all(ctx().db_path, user_id("alice")) == []
    assert pins_store.get_all(ctx().db_path, user_id("alice")) == []
    assert public_links.get_token(ctx().db_path, user_id("alice")) is None
    assert not shared_maps.is_shared(ctx().db_path, user_id("alice"))


@pytest.mark.parametrize(
    "headers",
    [{"sec-fetch-site": "same-origin"}, {"origin": "http://testserver"}, {}],
)
def test_same_origin_mutation_allowed(
    client: TestClient, headers: dict[str, str]
) -> None:
    resp = client.post(
        "/api/auth/register",
        json={"username": "carol", "password": PASSWORD},
        headers=headers,
    )
    assert resp.status_code == 201


# ── Every data endpoint needs a session ──────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/pins"),
        ("GET", "/api/routes"),
        ("GET", "/api/ratings"),
        ("PUT", "/api/ratings/r1"),
        ("POST", "/api/routes"),
        ("DELETE", "/api/routes/r1"),
        ("PATCH", "/api/routes/r1"),
        ("POST", "/api/pins"),
        ("PATCH", "/api/pins/p1"),
        ("DELETE", "/api/pins/p1"),
        ("GET", "/api/public-link"),
        ("POST", "/api/public-link"),
        ("DELETE", "/api/public-link"),
        ("POST", "/api/sync"),
        ("GET", "/api/sync/status"),
        ("POST", "/api/garmin/mfa"),
        ("GET", "/api/integrations/garmin"),
        ("PUT", "/api/integrations/garmin"),
        ("DELETE", "/api/integrations/garmin"),
        ("POST", "/api/integrations/strava/archive"),
    ],
)
def test_data_endpoints_require_login(client: TestClient, method: str, path: str) -> None:
    resp = client.request(method, path, json={"fun": 1, "code": "1"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "not_authenticated"


def test_index_and_config_are_public(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/api/config").status_code == 200


# ── Frontend wiring ──────────────────────────────────────────────────


def test_index_ships_login_ui_and_api_plumbing(client: TestClient) -> None:
    html = client.get("/").text
    assert 'id="auth-overlay"' in html
    assert 'id="auth-tab-login"' in html and 'id="auth-tab-register"' in html
    assert 'id="logout-button"' in html
    assert "document.body.dataset.apiBase || '/api'" in html
    assert "async function apiFetch(path, opts)" in html
    # Every server call goes through API_BASE: no hard-coded /api/ fetches.
    assert "fetch('/api/" not in html and "fetch(`/api/" not in html

