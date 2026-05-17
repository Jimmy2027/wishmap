"""HTTP integration tests for the /api/sync endpoints.

These tests mock out `sync_runner.run_sync` so no actual sync runs — the
focus is on the endpoint contract (202 vs 409, response shape, idempotent
spam handling, the vault requirement, what the run is handed, one status
per user). The runner's own bookkeeping is covered by test_sync_runner.py,
real syncs over HTTP by test_integrations.py.

Run on the shared fixtures (conftest.py): `client` is signed in as alice
here; each TestClient lifespan builds a fresh app context, so no sync
status bleeds between tests.
"""

import json
from collections.abc import Coroutine
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ctx, user_id, user_state
from wishmap import integrations_store, sync_runner, vault
from wishmap.app import app
from wishmap.auth import SESSION_COOKIE, VAULT_COOKIE
from wishmap.garmin import GarminSyncContext


@pytest.fixture(autouse=True)
def noop_run_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace run_sync with a no-op coroutine so endpoint tests don't kick
    # off a real Garmin sync. State is still flipped by begin()
    # synchronously inside the POST handler, so 202/409 semantics work.
    async def noop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(sync_runner, "run_sync", noop)


@pytest.fixture
def client(user_client: TestClient) -> TestClient:
    """This module acts as alice throughout."""
    return user_client


def _record_run_sync(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    seen: list[tuple[Any, ...]] = []

    async def noop() -> None:
        return None

    # A plain function (not `async def`) so the args are recorded while the
    # handler builds the coroutine, not whenever the task gets scheduled.
    def record_run_sync(*args: Any) -> Coroutine[Any, Any, None]:
        seen.append(args)
        return noop()

    monkeypatch.setattr(sync_runner, "run_sync", record_run_sync)
    return seen


def _set_garmin(c: TestClient, **settings: Any) -> None:
    resp = c.put(
        "/api/integrations/garmin", json={"username": "me@example.com", **settings}
    )
    assert resp.status_code == 200, resp.text


def test_initial_status_is_idle(client: TestClient) -> None:
    resp = client.get("/api/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "idle"
    assert body["started_at"] is None
    assert body["finished_at"] is None
    assert body["error"] is None
    assert set(body["services"].keys()) == {"garmin"}


def test_post_sync_returns_202_and_flips_state(client: TestClient) -> None:
    resp = client.post("/api/sync")
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "running"
    assert body["started_at"] is not None
    assert body["error"] is None


def test_second_post_returns_409_with_current_status(client: TestClient) -> None:
    first = client.post("/api/sync")
    assert first.status_code == 202

    second = client.post("/api/sync", json={"garmin_password": "pw"})
    assert second.status_code == 409
    body = second.json()
    assert body["state"] == "running"
    # Same started_at — the second call did NOT reset state.
    assert body["started_at"] == first.json()["started_at"]


def test_status_payload_shape(client: TestClient) -> None:
    client.post("/api/sync")
    resp = client.get("/api/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "state",
        "started_at",
        "finished_at",
        "services",
        "error",
        "needs_mfa",
        "needs_garmin_login",
    }
    assert body["needs_mfa"] is False
    assert body["needs_garmin_login"] is False
    assert set(body["services"]["garmin"].keys()) == {"state", "error"}


def test_status_endpoint_reflects_post(client: TestClient) -> None:
    before = client.get("/api/sync/status").json()
    assert before["state"] == "idle"

    client.post("/api/sync")

    after = client.get("/api/sync/status").json()
    assert after["state"] == "running"
    assert after["started_at"] is not None


def test_restart_starts_from_idle(client: TestClient) -> None:
    # A new lifespan builds a new state registry: a sync left "running" by
    # the previous app instance doesn't survive into the next one.
    assert client.post("/api/sync").status_code == 202
    with TestClient(app) as c2:
        c2.cookies.set(SESSION_COOKIE, client.cookies[SESSION_COOKIE])
        assert c2.get("/api/sync/status").json()["state"] == "idle"


def test_sync_without_garmin_account_runs_with_no_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_run_sync(monkeypatch)
    assert client.post("/api/sync").status_code == 202
    state = user_state("alice")
    assert seen == [(state, None)]
    assert state.sync_status["state"] == "running"


def test_sync_context_carries_settings_password_and_tokens(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_garmin(client, activity_limit=50, sport_filter=["hiking"])
    uid = user_id("alice")
    key = vault.decode_key(client.cookies[VAULT_COOKIE])
    assert key is not None
    tokens = json.dumps({"di_token": "t"})
    integrations_store.set_tokens(
        ctx().db_path,
        uid,
        "garmin",
        vault.seal(key, tokens.encode(), user_id=uid, provider="garmin"),
    )
    seen = _record_run_sync(monkeypatch)

    password = "s3cret-garmin-pw"
    resp = client.post("/api/sync", json={"garmin_password": password})
    assert resp.status_code == 202
    assert password not in resp.text

    ((state, sync_ctx),) = seen
    assert state is user_state("alice")
    assert isinstance(sync_ctx, GarminSyncContext)
    assert (sync_ctx.user_id, sync_ctx.username) == (uid, "me@example.com")
    assert (sync_ctx.activity_limit, sync_ctx.sport_filter) == (50, ["hiking"])
    assert sync_ctx.tokens_json == tokens
    assert sync_ctx.password == password
    assert sync_ctx.pending is state.pending_mfa
    assert sync_ctx.user_dir == ctx().user_dir(uid)
    # The path even before an archive exists: the sync checks it when matching.
    assert sync_ctx.strava_db == ctx().user_dir(uid) / "strava" / "activities.db"
    # Neither secret shows in the context's repr (tracebacks, debug logs).
    assert password not in repr(sync_ctx)
    assert tokens not in repr(sync_ctx)


def test_empty_password_counts_as_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_garmin(client)
    seen = _record_run_sync(monkeypatch)
    assert client.post("/api/sync", json={"garmin_password": ""}).status_code == 202
    assert seen[0][1].password is None


def test_oversized_password_is_422(client: TestClient) -> None:
    resp = client.post("/api/sync", json={"garmin_password": "x" * 1025})
    assert resp.status_code == 422
    assert user_state("alice").sync_status["state"] == "idle"


def test_sync_needs_the_vault_key(client: TestClient) -> None:
    client.cookies.delete(VAULT_COOKIE)
    resp = client.post("/api/sync")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "vault_locked"
    assert user_state("alice").sync_status["state"] == "idle"


def test_stale_vault_key_is_locked(client: TestClient) -> None:
    client.cookies.set(VAULT_COOKIE, vault.encode_key(vault.new_key()))
    resp = client.post("/api/sync")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "vault_locked"


def test_tokens_that_do_not_open_are_forgotten(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leftover tokens sealed under some other key can never be used: the
    sync drops them (Garmin then asks for a login) instead of failing."""
    _set_garmin(client)
    uid = user_id("alice")
    integrations_store.set_tokens(
        ctx().db_path,
        uid,
        "garmin",
        vault.seal(vault.new_key(), b"{}", user_id=uid, provider="garmin"),
    )
    seen = _record_run_sync(monkeypatch)
    assert client.post("/api/sync").status_code == 202
    assert seen[0][1].tokens_json is None
    integration = integrations_store.get(ctx().db_path, uid, "garmin")
    assert integration is not None and integration.tokens_enc is None


def test_sync_status_is_per_user(
    client: TestClient, second_user_client: TestClient
) -> None:
    assert client.post("/api/sync").status_code == 202
    # Alice's running sync doesn't show up for bob.
    assert second_user_client.get("/api/sync/status").json()["state"] == "idle"
    assert user_state("alice") is not user_state("bob")


def test_every_user_syncs_their_own_account(
    client: TestClient,
    second_user_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_garmin(client)
    _set_garmin(second_user_client, activity_limit=7)
    seen = _record_run_sync(monkeypatch)
    assert client.post("/api/sync").status_code == 202
    assert second_user_client.post("/api/sync").status_code == 202
    (alice_state, alice_ctx), (bob_state, bob_ctx) = seen
    assert alice_state is user_state("alice") and bob_state is user_state("bob")
    assert (alice_ctx.user_id, bob_ctx.user_id) == (user_id("alice"), user_id("bob"))
    assert bob_ctx.activity_limit == 7
    assert alice_ctx.pending is not bob_ctx.pending
