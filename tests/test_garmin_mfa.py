"""Tests for the Garmin login (tokens, password, MFA), the sync itself, and
POST /api/garmin/mfa.

garminconnect.Garmin is replaced by `FakeGarmin` so nothing touches the
network. A pending MFA login is parked on a per-user `PendingMfaSlot`; the
unit tests use a fresh slot, the endpoint tests a signed-in user's (see
conftest.py). The full web flow (settings → sync → login → MFA → sync) is
in test_integrations.py.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from garminconnect import GarminConnectAuthenticationError

from tests.fake_garmin import (
    GARMIN_PASSWORD,
    GARMIN_USER,
    GOOD_CODE,
    FakeGarmin,
    activity,
)
from tests.helpers import ctx, user_id, user_state
from wishmap import garmin, integrations_store, routes_store, strava, vault
from wishmap.app import app
from wishmap.auth import SESSION_COOKIE, VAULT_COOKIE
from wishmap.exceptions import GarminLoginRequiredError, MfaRequiredError, SyncError
from wishmap.garmin import GarminSyncContext
from wishmap.state import PendingMfaSlot, StateRegistry, UserState


@pytest.fixture(autouse=True)
def fake_garmin(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGarmin.reset()
    monkeypatch.setattr(garmin, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin.time, "sleep", lambda s: None)
    monkeypatch.delenv("GARMINTOKENS", raising=False)


@pytest.fixture
def pending() -> PendingMfaSlot:
    return PendingMfaSlot()


class _Tokens:
    """Collects what a sync hands to `on_tokens`."""

    def __init__(self) -> None:
        self.saved: list[str] = []

    def __call__(self, tokens: str) -> None:
        self.saved.append(tokens)


def _ctx(
    tmp_path: Path,
    pending: PendingMfaSlot,
    *,
    tokens_json: str | None = None,
    password: str | None = None,
    sink: _Tokens | None = None,
    **kw: object,
) -> GarminSyncContext:
    return GarminSyncContext(
        user_id=1,
        db_path=tmp_path / "wishmap.db",
        user_dir=tmp_path / "users" / "1",
        username=GARMIN_USER,
        tokens_json=tokens_json,
        password=password,
        pending=pending,
        on_tokens=sink or _Tokens(),
        **kw,  # type: ignore[arg-type]
    )


def _token_of(tokens_json: str) -> str:
    return json.loads(tokens_json)["di_token"]


# ── Login ────────────────────────────────────────────────────────────


def test_fresh_login_without_mfa(tmp_path: Path, pending: PendingMfaSlot) -> None:
    client = garmin._authenticate(_ctx(tmp_path, pending, password=GARMIN_PASSWORD))

    cred = FakeGarmin.credential_logins()
    assert len(cred) == 1
    assert cred[0].login_args == [None]
    assert cred[0].return_on_mfa is True
    # The password isn't kept on the client once the login is done.
    assert cred[0].password is None
    # Ends with a token-based login (padded token string) so the profile loads.
    assert client.username is None
    assert client.client.di_token in FakeGarmin.valid_tokens
    assert not pending.is_set()


def test_saved_tokens_need_no_password(tmp_path: Path, pending: PendingMfaSlot) -> None:
    FakeGarmin.valid_tokens.add("saved")
    tokens = json.dumps({"di_token": "saved"})
    assert len(tokens) < 512  # so the padding is exercised
    client = garmin._authenticate(_ctx(tmp_path, pending, tokens_json=tokens))
    assert client.client.di_token == "saved"
    assert FakeGarmin.credential_logins() == []
    login_arg = FakeGarmin.instances[0].login_args[0]
    assert login_arg is not None and login_arg.strip() == tokens


def test_saved_tokens_win_over_a_password(tmp_path: Path, pending: PendingMfaSlot) -> None:
    FakeGarmin.valid_tokens.add("saved")
    garmin._authenticate(
        _ctx(
            tmp_path,
            pending,
            tokens_json=json.dumps({"di_token": "saved"}),
            password=GARMIN_PASSWORD,
        )
    )
    assert FakeGarmin.credential_logins() == []


def test_rejected_tokens_fall_back_to_the_password(
    tmp_path: Path, pending: PendingMfaSlot
) -> None:
    client = garmin._authenticate(
        _ctx(
            tmp_path,
            pending,
            tokens_json=json.dumps({"di_token": "expired"}),
            password=GARMIN_PASSWORD,
        )
    )
    assert len(FakeGarmin.credential_logins()) == 1
    assert client.client.di_token in FakeGarmin.valid_tokens


def test_no_tokens_and_no_password_needs_a_login(
    tmp_path: Path, pending: PendingMfaSlot
) -> None:
    with pytest.raises(GarminLoginRequiredError, match="Log in to Garmin"):
        garmin._authenticate(_ctx(tmp_path, pending))
    assert FakeGarmin.instances == []


def test_rejected_tokens_and_no_password_needs_a_login(
    tmp_path: Path, pending: PendingMfaSlot
) -> None:
    with pytest.raises(GarminLoginRequiredError, match="password"):
        garmin._authenticate(
            _ctx(tmp_path, pending, tokens_json=json.dumps({"di_token": "expired"}))
        )
    assert FakeGarmin.credential_logins() == []


def test_wrong_password_needs_a_login(tmp_path: Path, pending: PendingMfaSlot) -> None:
    with pytest.raises(GarminLoginRequiredError, match="rejected") as exc:
        garmin._authenticate(_ctx(tmp_path, pending, password="wrong"))
    assert "wrong" not in str(exc.value)
    assert not pending.is_set()


def test_garmintokens_env_is_refused(
    tmp_path: Path, pending: PendingMfaSlot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """garminconnect would log in with the env's tokens instead of the user's."""
    monkeypatch.setenv("GARMINTOKENS", str(tmp_path / "host-tokens"))
    with pytest.raises(SyncError, match="GARMINTOKENS"):
        garmin._authenticate(_ctx(tmp_path, pending, password=GARMIN_PASSWORD))
    assert FakeGarmin.instances == []


def test_dump_tokens_skips_sessions_without_tokens() -> None:
    client = FakeGarmin()
    assert garmin.dump_tokens(client) is None  # type: ignore[arg-type]
    client.client.di_token = "t"
    tokens = garmin.dump_tokens(client)  # type: ignore[arg-type]
    assert tokens is not None and _token_of(tokens) == "t"


# ── MFA ──────────────────────────────────────────────────────────────


def test_mfa_required_then_complete(tmp_path: Path, pending: PendingMfaSlot) -> None:
    FakeGarmin.needs_mfa = True
    with pytest.raises(MfaRequiredError):
        garmin._authenticate(_ctx(tmp_path, pending, password=GARMIN_PASSWORD))
    assert pending.is_set()
    parked = FakeGarmin.credential_logins()[0]
    assert parked.password is None  # the parked login doesn't hold the password

    tokens = garmin.complete_mfa(pending, f" {GOOD_CODE} ", GARMIN_USER)

    assert tokens is not None and _token_of(tokens) in FakeGarmin.valid_tokens
    assert not pending.is_set()
    # The next sync logs in from those tokens without any password or MFA.
    garmin._authenticate(_ctx(tmp_path, pending, tokens_json=tokens))
    assert len(FakeGarmin.credential_logins()) == 1


def test_wrong_mfa_code_keeps_pending_login(
    tmp_path: Path, pending: PendingMfaSlot
) -> None:
    FakeGarmin.needs_mfa = True
    with pytest.raises(MfaRequiredError):
        garmin._authenticate(_ctx(tmp_path, pending, password=GARMIN_PASSWORD))

    with pytest.raises(GarminConnectAuthenticationError):
        garmin.complete_mfa(pending, "000000", GARMIN_USER)
    assert pending.is_set()

    assert garmin.complete_mfa(pending, GOOD_CODE, GARMIN_USER) is not None
    assert not pending.is_set()


def test_complete_mfa_without_pending_login(pending: PendingMfaSlot) -> None:
    with pytest.raises(SyncError):
        garmin.complete_mfa(pending, GOOD_CODE, GARMIN_USER)


def test_expired_pending_login_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending: PendingMfaSlot
) -> None:
    FakeGarmin.needs_mfa = True
    with pytest.raises(MfaRequiredError):
        garmin._authenticate(_ctx(tmp_path, pending, password=GARMIN_PASSWORD))
    monkeypatch.setattr(garmin, "MFA_PENDING_TTL_SEC", -1)
    with pytest.raises(SyncError):
        garmin.complete_mfa(pending, GOOD_CODE, GARMIN_USER)
    assert not pending.is_set()


def test_sync_propagates_mfa_required(tmp_path: Path, pending: PendingMfaSlot) -> None:
    """sync() must not wrap MfaRequiredError into a generic auth failure."""
    FakeGarmin.needs_mfa = True
    with pytest.raises(MfaRequiredError):
        garmin.sync(_ctx(tmp_path, pending, password=GARMIN_PASSWORD))
    assert pending.is_set()


def test_pending_logins_are_per_user(tmp_path: Path) -> None:
    """One user's parked login can't be completed (or seen) through another's slot."""
    FakeGarmin.needs_mfa = True
    registry = StateRegistry()
    alice, bob = registry.get(1), registry.get(2)
    with pytest.raises(MfaRequiredError):
        garmin._authenticate(
            _ctx(tmp_path, alice.pending_mfa, password=GARMIN_PASSWORD)
        )

    assert alice.pending_mfa.is_set()
    assert not bob.pending_mfa.is_set()
    with pytest.raises(SyncError):
        garmin.complete_mfa(bob.pending_mfa, GOOD_CODE, GARMIN_USER)
    assert alice.pending_mfa.is_set()

    garmin.complete_mfa(alice.pending_mfa, GOOD_CODE, GARMIN_USER)
    assert not alice.pending_mfa.is_set()


def test_login_parked_for_another_account_is_dropped(
    tmp_path: Path, pending: PendingMfaSlot
) -> None:
    """A code can't finish a login for an account other than the caller's."""
    FakeGarmin.needs_mfa = True
    with pytest.raises(MfaRequiredError):
        garmin._authenticate(_ctx(tmp_path, pending, password=GARMIN_PASSWORD))
    with pytest.raises(SyncError):
        garmin.complete_mfa(pending, GOOD_CODE, "other@example.com")
    assert not pending.is_set()


def test_newer_login_survives_completion_of_older() -> None:
    """complete_mfa only clears the slot if it still holds the login it resumed."""
    slot = PendingMfaSlot()
    older, newer = FakeGarmin(email="a"), FakeGarmin(email="a")
    slot.put(older, "a")  # type: ignore[arg-type]
    assert slot.get(60) == (older, "a")
    slot.put(newer, "a")  # type: ignore[arg-type]
    slot.discard(older)  # type: ignore[arg-type]
    assert slot.get(60) == (newer, "a")
    slot.clear()
    assert not slot.is_set()


# ── The sync ─────────────────────────────────────────────────────────


def test_sync_writes_routes_gpx_index_and_tokens(
    tmp_path: Path, pending: PendingMfaSlot
) -> None:
    FakeGarmin.activities = [
        activity(11, name="Morning hike", start_gmt="2026-06-01 08:00:00"),
        activity(12, name="Ride", type_key="road_biking"),
        {**activity(13), "startLatitude": None},  # no GPS: skipped
    ]
    sink = _Tokens()
    ctx_ = _ctx(tmp_path, pending, password=GARMIN_PASSWORD, sink=sink)
    garmin.sync(ctx_)

    routes = {r.id: r for r in routes_store.get_all(ctx_.db_path, 1, "garmin")}
    assert set(routes) == {"garmin-11", "garmin-12"}
    r11 = routes["garmin-11"]
    assert (r11.name, r11.sport, r11.status.value, r11.gpx) == (
        "Morning hike",
        ["hiking"],
        "done",
        "gpx/garmin/11.gpx",
    )
    assert r11.notes == "2026-06-01 | 12.0 km | 1h 30m"
    assert routes["garmin-12"].sport == ["biking"]
    assert (ctx_.user_dir / "gpx/garmin/11.gpx").is_file()
    index = garmin.load_activity_index(ctx_.user_dir)
    assert index is not None and [a["activityId"] for a in index] == [11, 12]
    assert "startLatitude" not in index[0]  # only what re-matching needs
    # One token hand-off for the login; unchanged tokens aren't re-saved.
    assert len(sink.saved) == 1
    assert _token_of(sink.saved[0]) in FakeGarmin.valid_tokens


def test_second_sync_uses_tokens_and_replaces_the_route_set(
    tmp_path: Path, pending: PendingMfaSlot
) -> None:
    FakeGarmin.activities = [activity(11), activity(12)]
    sink = _Tokens()
    garmin.sync(_ctx(tmp_path, pending, password=GARMIN_PASSWORD, sink=sink))
    first_downloads = sum(len(c.downloads) for c in FakeGarmin.instances)

    # Activity 11 drops out of the limit; 13 is new.
    FakeGarmin.activities = [activity(13), activity(12)]
    garmin.sync(
        _ctx(tmp_path, pending, tokens_json=sink.saved[-1], sink=sink, activity_limit=2)
    )
    assert len(FakeGarmin.credential_logins()) == 1
    ids = {r.id for r in routes_store.get_all(tmp_path / "wishmap.db", 1, "garmin")}
    assert ids == {"garmin-12", "garmin-13"}
    # Existing GPX files aren't downloaded again.
    assert sum(len(c.downloads) for c in FakeGarmin.instances) == first_downloads + 1


def test_refreshed_tokens_are_saved(tmp_path: Path, pending: PendingMfaSlot) -> None:
    FakeGarmin.valid_tokens.add("old")
    FakeGarmin.refresh_on_token_login = True
    sink = _Tokens()
    garmin.sync(_ctx(tmp_path, pending, tokens_json=json.dumps({"di_token": "old"}), sink=sink))
    assert len(sink.saved) == 1
    assert _token_of(sink.saved[0]) != "old"
    assert _token_of(sink.saved[0]) in FakeGarmin.valid_tokens


def test_sync_matches_strava(tmp_path: Path, pending: PendingMfaSlot) -> None:
    strava_db = tmp_path / "strava.db"
    csv = tmp_path / "activities.csv"
    csv.write_text(
        "Activity ID,Activity Date,Activity Name,Activity Type,Distance,Filename\n"
        '77,"Jun 1, 2026, 8:01:00 AM",Strava name,Hike,12100.0,a.gpx\n'
    )
    strava.import_archive(strava_db, csv)
    FakeGarmin.activities = [activity(11), activity(12, start_gmt="2026-01-01 08:00:00")]
    garmin.sync(
        _ctx(tmp_path, pending, password=GARMIN_PASSWORD, strava_db=strava_db)
    )
    routes = {r.id: r for r in routes_store.get_all(tmp_path / "wishmap.db", 1)}
    assert (routes["garmin-11"].name, routes["garmin-11"].strava_id) == ("Strava name", 77)
    assert routes["garmin-12"].strava_id is None


def test_rematch_strava(tmp_path: Path, pending: PendingMfaSlot) -> None:
    db_path, udir = tmp_path / "wishmap.db", tmp_path / "users" / "1"
    strava_db = tmp_path / "strava.db"
    # No sync yet: nothing to match, nothing changed.
    assert garmin.rematch_strava(db_path, 1, udir, strava_db) is None

    FakeGarmin.activities = [activity(11)]
    garmin.sync(_ctx(tmp_path, pending, password=GARMIN_PASSWORD))
    assert garmin.rematch_strava(db_path, 1, udir, strava_db) == 0

    csv = tmp_path / "activities.csv"
    csv.write_text(
        "Activity ID,Activity Date,Activity Name,Activity Type,Distance\n"
        '77,"Jun 1, 2026, 8:00:30 AM",From Strava,Backcountry Ski,12000\n'
    )
    strava.import_archive(strava_db, csv)
    assert garmin.rematch_strava(db_path, 1, udir, strava_db) == 1
    (route,) = routes_store.get_all(db_path, 1, "garmin")
    assert (route.name, route.sport, route.strava_id) == ("From Strava", ["ski_touring"], 77)


def test_unreadable_activity_index_is_ignored(tmp_path: Path) -> None:
    udir = tmp_path / "u"
    (udir / garmin.ACTIVITY_INDEX).parent.mkdir(parents=True)
    (udir / garmin.ACTIVITY_INDEX).write_text("not json")
    assert garmin.load_activity_index(udir) is None


# ── _fetch_activities pagination ─────────────────────────────────────


class _PagedClient:
    def __init__(self, total: int) -> None:
        self.total = total
        self.calls: list[tuple[int, int]] = []

    def get_activities(self, start: int, limit: int) -> list[dict[str, object]]:
        self.calls.append((start, limit))
        end = min(start + limit, self.total)
        return [
            {"activityId": i, "startLatitude": 1.0, "activityType": {"typeKey": "hiking" if i % 2 else "running"}}
            for i in range(start, end)
        ]


def test_fetch_activities_pages_past_api_cap() -> None:
    client = _PagedClient(total=2500)
    acts = garmin._fetch_activities(client, 5000)  # type: ignore[arg-type]
    assert len(acts) == 2500
    assert client.calls == [(0, 1000), (1000, 1000), (2000, 1000)]


def test_fetch_activities_respects_limit() -> None:
    client = _PagedClient(total=2500)
    acts = garmin._fetch_activities(client, 1200)  # type: ignore[arg-type]
    assert len(acts) == 1200
    assert client.calls == [(0, 1000), (1000, 200)]


def test_fetch_activities_sport_filter() -> None:
    client = _PagedClient(total=10)
    acts = garmin._fetch_activities(client, 10, ["hiking"])  # type: ignore[arg-type]
    assert [a["activityId"] for a in acts] == [1, 3, 5, 7, 9]


# ── POST /api/garmin/mfa ─────────────────────────────────────────────


@pytest.fixture
def alice(user_client: TestClient) -> TestClient:
    """The endpoint tests act as alice, with a Garmin account set."""
    resp = user_client.put("/api/integrations/garmin", json={"username": GARMIN_USER})
    assert resp.status_code == 200
    return user_client


def _start_pending_login(username: str = "alice") -> UserState:
    """Park a login on a user's slot, as a sync hitting MFA would."""
    FakeGarmin.needs_mfa = True
    state = user_state(username)
    sync_ctx = GarminSyncContext(
        user_id=state.user_id,
        db_path=ctx().db_path,
        user_dir=ctx().user_dir(state.user_id),
        username=GARMIN_USER,
        password=GARMIN_PASSWORD,
        pending=state.pending_mfa,
    )
    with pytest.raises(MfaRequiredError):
        garmin._authenticate(sync_ctx)
    return state


def _stored_tokens(c: TestClient, username: str = "alice") -> str | None:
    uid = user_id(username)
    integration = integrations_store.get(ctx().db_path, uid, "garmin")
    assert integration is not None
    if integration.tokens_enc is None:
        return None
    key = vault.decode_key(c.cookies[VAULT_COOKIE])
    assert key is not None
    return vault.unseal(key, integration.tokens_enc, user_id=uid, provider="garmin").decode()


def test_endpoint_no_pending_login(alice: TestClient) -> None:
    resp = alice.post("/api/garmin/mfa", json={"code": GOOD_CODE})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "no_pending_login"


def test_endpoint_bad_then_good_code(alice: TestClient) -> None:
    state = _start_pending_login()

    resp = alice.post("/api/garmin/mfa", json={"code": "000000"})
    assert resp.status_code == 401
    assert resp.json()["reason"] == "bad_code"
    # A bad code is not a lost session (the web UI tells the two apart).
    assert alice.get("/api/me").status_code == 200
    assert _stored_tokens(alice) is None

    resp = alice.post("/api/garmin/mfa", json={"code": GOOD_CODE})
    assert resp.status_code == 204
    assert not state.pending_mfa.is_set()
    # The new session is stored, sealed with alice's vault key.
    tokens = _stored_tokens(alice)
    assert tokens is not None and _token_of(tokens) in FakeGarmin.valid_tokens
    assert alice.get("/api/integrations/garmin").json()["logged_in"] is True


def test_endpoint_needs_the_vault_key(alice: TestClient) -> None:
    state = _start_pending_login()
    alice.cookies.delete(VAULT_COOKIE)
    resp = alice.post("/api/garmin/mfa", json={"code": GOOD_CODE})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "vault_locked"
    assert state.pending_mfa.is_set()


def test_endpoint_without_garmin_account(user_client: TestClient) -> None:
    _start_pending_login()
    resp = user_client.post("/api/garmin/mfa", json={"code": GOOD_CODE})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "no_garmin_config"


def test_endpoint_rejects_cross_origin(alice: TestClient) -> None:
    state = _start_pending_login()
    resp = alice.post(
        "/api/garmin/mfa",
        json={"code": GOOD_CODE},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert resp.status_code == 403
    assert state.pending_mfa.is_set()


def test_endpoint_requires_login(alice: TestClient) -> None:
    state = _start_pending_login()
    anonymous = TestClient(app)
    assert anonymous.post("/api/garmin/mfa", json={"code": GOOD_CODE}).status_code == 401
    assert state.pending_mfa.is_set()


def test_endpoint_ignores_login_parked_on_another_user(
    alice: TestClient, second_user_client: TestClient
) -> None:
    bob = _start_pending_login("bob")
    resp = alice.post("/api/garmin/mfa", json={"code": GOOD_CODE})
    assert resp.status_code == 409
    assert bob.pending_mfa.is_set()


def test_switching_garmin_account_drops_the_pending_login(alice: TestClient) -> None:
    state = _start_pending_login()
    # Re-saving the same account keeps it…
    alice.put("/api/integrations/garmin", json={"username": GARMIN_USER, "activity_limit": 5})
    assert state.pending_mfa.is_set()
    # …another account doesn't.
    alice.put("/api/integrations/garmin", json={"username": "other@example.com"})
    assert not state.pending_mfa.is_set()


def test_login_parked_after_an_account_switch_is_refused(alice: TestClient) -> None:
    """A slow login for the old account, parked only after the user switched
    accounts, must not save its tokens onto the new account's row."""
    state = user_state("alice")
    sync_ctx = GarminSyncContext(
        user_id=state.user_id,
        db_path=ctx().db_path,
        user_dir=ctx().user_dir(state.user_id),
        username=GARMIN_USER,
        password=GARMIN_PASSWORD,
        pending=state.pending_mfa,
    )
    alice.put("/api/integrations/garmin", json={"username": "other@example.com"})
    FakeGarmin.needs_mfa = True
    with pytest.raises(MfaRequiredError):
        garmin._authenticate(sync_ctx)  # parks GARMIN_USER's login

    resp = alice.post("/api/garmin/mfa", json={"code": GOOD_CODE})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "no_pending_login"
    assert not state.pending_mfa.is_set()
    assert _stored_tokens(alice) is None
    assert alice.get("/api/integrations/garmin").json()["logged_in"] is False


def test_restart_drops_pending_login(alice: TestClient) -> None:
    _start_pending_login()
    with TestClient(app) as c2:
        c2.cookies.set(SESSION_COOKIE, alice.cookies[SESSION_COOKIE])
        c2.cookies.set(VAULT_COOKIE, alice.cookies[VAULT_COOKIE])
        resp = c2.post("/api/garmin/mfa", json={"code": GOOD_CODE})
        assert resp.status_code == 409
