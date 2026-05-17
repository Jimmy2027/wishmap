"""HTTP tests for a user's integrations: Garmin settings and login, the real
Garmin sync driven over the web API (with a fake Garmin), and the Strava
archive upload.

The end-to-end tests run the actual background sync (`sync_runner.run_sync`
on the TestClient's event loop) against `FakeGarmin`, and check that the
Garmin password and session tokens never reach the DB, the data files or
the logs in the clear.

A background sync only survives on the `client` fixture's TestClient: it
was entered as a context manager, so its event loop outlives a request,
while a bare TestClient (like `signed_in_client`'s) runs each request on a
loop of its own and cancels the sync task with it. So every sync is driven
through `client`, with the cookies of the browser it stands for.
"""

import io
import json
import logging
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fake_garmin import (
    GARMIN_PASSWORD,
    GARMIN_USER,
    GOOD_CODE,
    FakeGarmin,
    activity,
)
from tests.helpers import (
    MINIMAL_GPX,
    PASSWORD,
    ctx,
    signed_in_client,
    user_id,
    user_state,
)
from wishmap import app as app_module
from wishmap import garmin, integrations_store, routes_store, sync_runner
from wishmap.app import app
from wishmap.auth import VAULT_COOKIE

STRAVA_CSV = (
    "Activity ID,Activity Date,Activity Name,Activity Type,Distance,Filename\n"
    '77,"Jun 1, 2026, 8:01:00 AM",Strava name,Backcountry Ski,12100.0,a.fit.gz\n'
    '78,"Jun 3, 2026, 9:00:00 AM",Unmatched,Ride,5000.0,b.fit.gz\n'
)


@pytest.fixture(autouse=True)
def fake_garmin(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGarmin.reset()
    FakeGarmin.activities = [
        activity(11, name="Garmin hike", start_gmt="2026-06-01 08:00:00"),
        activity(12, name="Garmin ride", type_key="cycling", start_gmt="2026-06-02 08:00:00"),
    ]
    monkeypatch.setattr(garmin, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin.time, "sleep", lambda s: None)
    monkeypatch.delenv("GARMINTOKENS", raising=False)


@pytest.fixture
def alice(client: TestClient) -> TestClient:
    """The (context-entered) `client`, registered and signed in as alice."""
    resp = client.post(
        "/api/auth/register", json={"username": "alice", "password": PASSWORD}
    )
    assert resp.status_code == 201
    return client


@contextmanager
def _browser(runner: TestClient, browser: TestClient) -> Iterator[TestClient]:
    """Let `runner` (the entered client) act with `browser`'s cookies."""
    saved = list(runner.cookies.jar)
    runner.cookies.clear()
    for cookie in browser.cookies.jar:
        runner.cookies.jar.set_cookie(cookie)
    try:
        yield runner
    finally:
        runner.cookies.clear()
        for cookie in saved:
            runner.cookies.jar.set_cookie(cookie)


def _put_garmin(c: TestClient, **body: Any) -> Any:
    return c.put("/api/integrations/garmin", json={"username": GARMIN_USER, **body})


def _sync(c: TestClient, password: str | None = None) -> dict[str, Any]:
    """POST /api/sync and wait for the background run to finish."""
    body = None if password is None else {"garmin_password": password}
    resp = c.post("/api/sync", json=body)
    assert resp.status_code == 202, resp.text
    deadline = time.monotonic() + 10
    while True:
        status = c.get("/api/sync/status").json()
        if status["state"] != "running":
            return status
        assert time.monotonic() < deadline, "sync did not finish"
        time.sleep(0.01)


def _route_ids(c: TestClient) -> set[str]:
    return {f["properties"]["id"] for f in c.get("/api/routes").json()["features"]}


def _upload(c: TestClient, data: bytes, name: str = "activities.csv") -> Any:
    return c.post(
        "/api/integrations/strava/archive",
        files={"archive": (name, data, "application/octet-stream")},
    )


# ── Garmin settings ──────────────────────────────────────────────────


def test_garmin_defaults_before_setup(alice: TestClient) -> None:
    assert alice.get("/api/integrations/garmin").json() == {
        "username": None,
        "activity_limit": 1000,
        "sport_filter": [],
        "logged_in": False,
    }


def test_put_and_get_garmin_settings(alice: TestClient) -> None:
    resp = _put_garmin(
        alice, username=f"  {GARMIN_USER} ", activity_limit=5000,
        sport_filter=["hiking", " hiking", "", "cycling"],
    )
    assert resp.status_code == 200
    expected = {
        "username": GARMIN_USER,
        "activity_limit": 5000,
        "sport_filter": ["hiking", "cycling"],
        "logged_in": False,
    }
    assert resp.json() == expected
    assert alice.get("/api/integrations/garmin").json() == expected
    assert alice.get("/api/me").json()["integrations"] == {
        "garmin": {"username": GARMIN_USER, "logged_in": False}
    }


@pytest.mark.parametrize(
    "body",
    [
        {"username": ""},
        {"username": "   "},
        {"username": "x" * 255},
        {"username": GARMIN_USER, "activity_limit": 0},
        {"username": GARMIN_USER, "activity_limit": 20001},
        {"username": GARMIN_USER, "sport_filter": ["x" * 65]},
        {"activity_limit": 10},
    ],
)
def test_invalid_garmin_settings_are_422(alice: TestClient, body: dict[str, Any]) -> None:
    assert alice.put("/api/integrations/garmin", json=body).status_code == 422
    assert integrations_store.get(ctx().db_path, user_id("alice"), "garmin") is None


def test_garmin_settings_are_per_user(alice: TestClient) -> None:
    bob = signed_in_client("bob")
    _put_garmin(alice)
    assert bob.get("/api/integrations/garmin").json()["username"] is None
    assert bob.get("/api/me").json()["integrations"] == {}


# ── Garmin login over the web: the whole flow ────────────────────────


def test_sync_without_account_asks_for_a_garmin_login(alice: TestClient) -> None:
    status = _sync(alice)
    assert status["needs_garmin_login"] is True
    assert status["services"]["garmin"]["state"] == "needs_garmin_login"
    assert FakeGarmin.instances == []


def test_full_garmin_flow_never_leaks_secrets(
    alice: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    FakeGarmin.needs_mfa = True
    uid = user_id("alice")
    assert _put_garmin(alice).status_code == 200

    # 1. No saved session: the UI is asked for the Garmin password.
    status = _sync(alice)
    assert status["needs_garmin_login"] is True
    assert "Log in to Garmin" in status["services"]["garmin"]["error"]

    # 2. A wrong password: asked again, with Garmin's verdict.
    status = _sync(alice, "wrong garmin password")
    assert status["needs_garmin_login"] is True
    assert "rejected" in status["services"]["garmin"]["error"]

    # 3. The right one: Garmin wants an MFA code.
    status = _sync(alice, GARMIN_PASSWORD)
    assert status["needs_mfa"] is True and status["needs_garmin_login"] is False
    assert user_state("alice").pending_mfa.is_set()

    # 4. The code: the session is sealed into the DB.
    assert alice.post("/api/garmin/mfa", json={"code": GOOD_CODE}).status_code == 204
    assert alice.get("/api/integrations/garmin").json()["logged_in"] is True

    # 5. Sync again, no password: logs in from the saved session.
    status = _sync(alice)
    assert status["state"] == "done", status
    assert _route_ids(alice) == {"garmin-11", "garmin-12"}
    assert len(FakeGarmin.credential_logins()) == 2  # steps 2 and 3 only

    # The password, and the session tokens, are nowhere in the clear.
    integration = integrations_store.get(ctx().db_path, uid, "garmin")
    assert integration is not None and integration.tokens_enc is not None
    tokens = [t for t in FakeGarmin.valid_tokens]
    assert tokens
    secrets = [GARMIN_PASSWORD.encode(), b"wrong garmin password"]
    secrets += [t.encode() for t in tokens]
    for path in ctx().data_root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for secret in secrets:
                assert secret not in data, (path, secret)
    for secret in secrets:
        assert secret.decode() not in caplog.text
        assert secret.decode() not in json.dumps(alice.get("/api/sync/status").json())
    assert "di_token" not in caplog.text


def test_saved_session_works_from_a_new_browser(alice: TestClient) -> None:
    """The vault key is recovered at login, so another device can sync
    with the stored Garmin session — no Garmin password needed."""
    _put_garmin(alice)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"

    laptop = TestClient(app)
    resp = laptop.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert resp.status_code == 200
    FakeGarmin.activities.append(activity(13))
    with _browser(alice, laptop):
        assert _sync(alice)["state"] == "done"
    assert len(FakeGarmin.credential_logins()) == 1
    assert "garmin-13" in _route_ids(laptop)


def test_password_change_keeps_the_garmin_session(alice: TestClient) -> None:
    _put_garmin(alice)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"
    resp = alice.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": "brand new password"},
    )
    assert resp.status_code == 204
    other = TestClient(app)
    other.post(
        "/api/auth/login", json={"username": "alice", "password": "brand new password"}
    )
    with _browser(alice, other):
        assert _sync(alice)["state"] == "done"
    assert len(FakeGarmin.credential_logins()) == 1


def test_expired_garmin_session_asks_for_the_password_again(alice: TestClient) -> None:
    _put_garmin(alice)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"
    FakeGarmin.valid_tokens.clear()  # Garmin revoked the session
    status = _sync(alice)
    assert status["needs_garmin_login"] is True
    assert "password" in status["services"]["garmin"]["error"]
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"


def test_forget_garmin_login(alice: TestClient) -> None:
    _put_garmin(alice, activity_limit=50)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"
    assert alice.delete("/api/integrations/garmin").status_code == 204
    body = alice.get("/api/integrations/garmin").json()
    assert body == {
        "username": GARMIN_USER,
        "activity_limit": 50,
        "sport_filter": [],
        "logged_in": False,
    }
    assert _sync(alice)["needs_garmin_login"] is True
    # Idempotent, and fine without any Garmin account.
    assert alice.delete("/api/integrations/garmin").status_code == 204


def test_switching_garmin_account_forgets_the_session(alice: TestClient) -> None:
    _put_garmin(alice)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"
    _put_garmin(alice, activity_limit=10)  # same account: session kept
    assert alice.get("/api/integrations/garmin").json()["logged_in"] is True
    _put_garmin(alice, username="other@example.com")
    assert alice.get("/api/integrations/garmin").json()["logged_in"] is False


def test_users_sync_their_own_garmin(alice: TestClient) -> None:
    bob = signed_in_client("bob")
    FakeGarmin.accounts["bob@example.com"] = "bobs garmin pw"
    _put_garmin(alice)
    _put_garmin(bob, username="bob@example.com")
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"
    with _browser(alice, bob):
        # Bob has no session of his own: Alice's doesn't help him.
        assert _sync(alice)["needs_garmin_login"] is True
        assert _route_ids(alice) == set()
        assert _sync(alice, "bobs garmin pw")["state"] == "done"
    assert _route_ids(bob) == {"garmin-11", "garmin-12"}
    assert _route_ids(alice) == {"garmin-11", "garmin-12"}
    # Separate files per user.
    assert (ctx().user_dir(user_id("bob")) / "gpx/garmin/11.gpx").is_file()


def test_ratings_survive_a_resync(alice: TestClient) -> None:
    _put_garmin(alice)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"
    assert alice.put("/api/ratings/garmin-11", json={"fun": 5}).status_code == 200
    assert _sync(alice)["state"] == "done"
    assert alice.get("/api/ratings").json()["garmin-11"]["fun"] == 5


# ── Strava archive upload ────────────────────────────────────────────


def test_archive_upload_before_any_sync(alice: TestClient) -> None:
    resp = _upload(alice, STRAVA_CSV.encode())
    assert resp.status_code == 200
    assert resp.json() == {"imported": 2, "total": 2, "matched_routes": None}
    assert (ctx().user_dir(user_id("alice")) / "strava/activities.db").is_file()
    assert alice.get("/api/me").json()["integrations"]["strava"] == {
        "username": None,
        "logged_in": False,
    }

    # The next Garmin sync matches against it.
    _put_garmin(alice)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"
    names = {
        f["properties"]["id"]: f["properties"]["name"]
        for f in alice.get("/api/routes").json()["features"]
    }
    assert names == {"garmin-11": "Strava name", "garmin-12": "Garmin ride"}


def test_archive_upload_rematches_synced_routes(alice: TestClient) -> None:
    _put_garmin(alice)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"
    assert alice.get("/api/routes").status_code == 200  # warm the map

    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr("export_123/activities.csv", STRAVA_CSV)
    resp = _upload(alice, zipped.getvalue(), "export_123.zip")
    assert resp.status_code == 200
    assert resp.json() == {"imported": 2, "total": 2, "matched_routes": 1}

    # Served at once (the warm map was invalidated), without a Garmin login.
    features = {
        f["properties"]["id"]: f["properties"]
        for f in alice.get("/api/routes").json()["features"]
    }
    assert features["garmin-11"]["name"] == "Strava name"
    assert features["garmin-11"]["sport"] == ["ski_touring"]
    assert features["garmin-11"]["strava_id"] == 77
    assert len(FakeGarmin.credential_logins()) == 1


def test_archive_upload_is_per_user(alice: TestClient) -> None:
    bob = signed_in_client("bob")
    assert _upload(alice, STRAVA_CSV.encode()).status_code == 200
    assert not (ctx().user_dir(user_id("bob")) / "strava").exists()
    assert _upload(bob, STRAVA_CSV.encode()).json()["total"] == 2


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"PK\x03\x04 this is not really a zip", "export.zip"),
        (b"Activity ID,Name\n1,x\n", "activities.csv"),
        (b"\xff\xfe\x00garbage", "activities.csv"),
    ],
)
def test_bad_archive_is_422(alice: TestClient, data: bytes, name: str) -> None:
    resp = _upload(alice, data, name)
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    assert integrations_store.get(ctx().db_path, user_id("alice"), "strava") is None


def test_oversized_archive_is_413(
    alice: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "MAX_STRAVA_ARCHIVE_BYTES", 10)
    assert _upload(alice, STRAVA_CSV.encode()).status_code == 413


def test_archive_upload_waits_for_a_running_sync(alice: TestClient) -> None:
    assert sync_runner.begin(user_state("alice"))
    resp = _upload(alice, STRAVA_CSV.encode())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "sync_running"


def test_sync_started_during_an_upload_waits_for_it(
    alice: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync POSTed mid-import runs after it and keeps its Strava matches.

    The sync's context is built before the upload creates the Strava DB;
    it must still match against it rather than overwrite the re-match.
    """
    _put_garmin(alice)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"

    events: list[str] = []
    importing, release, fetched = threading.Event(), threading.Event(), threading.Event()
    real_import = app_module.strava.import_archive

    def gated_import(db: Any, upload: Any) -> int:
        importing.set()
        assert release.wait(5)
        events.append("import")
        return real_import(db, upload)

    real_fetch = garmin._fetch_activities

    def recorded_fetch(*args: Any, **kwargs: Any) -> Any:
        events.append("fetch")
        fetched.set()
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(app_module.strava, "import_archive", gated_import)
    monkeypatch.setattr(garmin, "_fetch_activities", recorded_fetch)

    uploaded: list[Any] = []
    uploader = threading.Thread(
        target=lambda: uploaded.append(_upload(alice, STRAVA_CSV.encode()))
    )
    uploader.start()
    assert importing.wait(5)
    resp = alice.post("/api/sync")
    assert resp.status_code == 202
    # Give the sync time to (wrongly) get going. (Not time.sleep: the
    # fake_garmin fixture turns that into a no-op.)
    assert not fetched.wait(0.3)
    release.set()
    uploader.join(5)
    assert uploaded[0].json()["matched_routes"] == 1

    deadline = time.monotonic() + 10
    while alice.get("/api/sync/status").json()["state"] == "running":
        assert time.monotonic() < deadline, "sync did not finish"
        time.sleep(0.01)
    assert alice.get("/api/sync/status").json()["state"] == "done"
    assert events == ["import", "fetch"]
    names = {
        f["properties"]["id"]: f["properties"]["name"]
        for f in alice.get("/api/routes").json()["features"]
    }
    assert names["garmin-11"] == "Strava name"


def test_archive_upload_needs_no_vault_key(alice: TestClient) -> None:
    """Strava data is not a secret: no vault needed to import it."""
    alice.cookies.delete(VAULT_COOKIE)
    assert _upload(alice, STRAVA_CSV.encode()).status_code == 200


def test_rematch_leaves_other_routes_alone(alice: TestClient) -> None:
    _put_garmin(alice)
    assert _sync(alice, GARMIN_PASSWORD)["state"] == "done"
    resp = alice.post(
        "/api/routes",
        data={"name": "Web route", "sport": "hiking", "status": "idea"},
        files={"gpx_file": ("r.gpx", MINIMAL_GPX, "application/gpx+xml")},
    )
    assert resp.status_code == 201
    assert _upload(alice, STRAVA_CSV.encode()).status_code == 200
    uid = user_id("alice")
    assert {r.id for r in routes_store.get_all(ctx().db_path, uid, "garmin")} == {
        "garmin-11",
        "garmin-12",
    }
    assert len(routes_store.get_all(ctx().db_path, uid, "web")) == 1


def test_index_ships_garmin_and_settings_ui(client: TestClient) -> None:
    html = client.get("/").text
    # A read-only copy hides the new overlays like every other edit control.
    assert "body.readonly .settings-overlay" in html
    for element in (
        'id="garmin-login-overlay"',
        'id="settings-overlay"',
        'id="settings-button"',
        'id="logout-button"',
        'id="password-form"',
        'id="garmin-settings-form"',
        'id="garmin-forget"',
        'id="strava-form"',
    ):
        assert element in html
    assert "needs_garmin_login" in html
    assert "vault_locked" in html
    assert "sync_not_allowed" not in html
