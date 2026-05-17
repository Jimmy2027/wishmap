"""Unit tests for the sync_runner state machine and error capture.

These tests monkeypatch `garmin.sync` so nothing touches the network.
Every test runs against its own fresh `UserState` — there is no module
state to reset. The goal here is to verify the runner's bookkeeping: state
transitions, error capture, the login/MFA signals, invalidation of the
user's map data, and per-user isolation. The real `garmin.sync` (with a
fake Garmin client) is covered by test_garmin_mfa.py and
test_integrations.py.

Async tests use `asyncio.run` directly rather than pytest-asyncio so this
test module needs no extra dependency.
"""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from wishmap import sync_runner
from wishmap.exceptions import GarminLoginRequiredError, MfaRequiredError, SyncError
from wishmap.garmin import GarminSyncContext
from wishmap.models import FeatureCollection
from wishmap.state import StateRegistry, UserData, UserState


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def state() -> UserState:
    return UserState(user_id=1)


def _warm(state: UserState) -> UserData:
    """Give the state built map data, so invalidation is observable."""
    data = UserData(
        pins_fc=FeatureCollection(features=[]),
        routes_fc=FeatureCollection(features=[]),
    )
    state.data = data
    return data


def _ctx(tmp_path: Path, state: UserState, password: str | None = None) -> GarminSyncContext:
    return GarminSyncContext(
        user_id=state.user_id,
        db_path=tmp_path / "wishmap.db",
        user_dir=tmp_path / "users" / str(state.user_id),
        username="u@example.com",
        password=password,
        pending=state.pending_mfa,
    )


def _garmin(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    monkeypatch.setattr("wishmap.garmin.sync", fn)


def test_initial_status_is_idle(state: UserState) -> None:
    status = sync_runner.get_status(state)
    assert status == {
        "state": "idle",
        "started_at": None,
        "finished_at": None,
        "services": {"garmin": {"state": "idle", "error": None}},
        "error": None,
        "needs_mfa": False,
        "needs_garmin_login": False,
    }


def test_get_status_is_a_snapshot(state: UserState) -> None:
    snap = sync_runner.get_status(state)
    snap["services"]["garmin"]["state"] = "mutated"
    assert sync_runner.get_status(state)["services"]["garmin"]["state"] == "idle"


def test_is_running_reflects_state(state: UserState) -> None:
    assert sync_runner.is_running(state) is False
    sync_runner.begin(state)
    assert sync_runner.is_running(state) is True


def test_begin_returns_false_when_already_running(state: UserState) -> None:
    assert sync_runner.begin(state) is True
    assert sync_runner.begin(state) is False


def test_begin_resets_started_at_and_clears_flags(state: UserState) -> None:
    state.sync_status["needs_mfa"] = True
    state.sync_status["needs_garmin_login"] = True
    sync_runner.begin(state)
    s = sync_runner.get_status(state)
    assert s["started_at"] is not None
    assert s["error"] is None
    assert s["needs_mfa"] is False and s["needs_garmin_login"] is False
    assert s["services"]["garmin"]["state"] == "idle"


def test_no_garmin_account_asks_for_a_login(state: UserState) -> None:
    data = _warm(state)
    sync_runner.begin(state)
    _run(sync_runner.run_sync(state, None))
    status = sync_runner.get_status(state)
    assert status["state"] == "error"
    assert status["needs_garmin_login"] is True
    assert status["services"]["garmin"] == {
        "state": "needs_garmin_login",
        "error": sync_runner.NO_GARMIN_ACCOUNT,
    }
    assert status["finished_at"] is not None
    # Nothing synced, so the warm map data is kept.
    assert state.data is data


def test_successful_sync_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: UserState
) -> None:
    seen: list[GarminSyncContext] = []
    _garmin(monkeypatch, seen.append)
    _warm(state)
    ctx = _ctx(tmp_path, state)
    sync_runner.begin(state)
    _run(sync_runner.run_sync(state, ctx))

    # Garmin runs on this user's context (and parks MFA on their slot).
    assert seen == [ctx]
    assert seen[0].pending is state.pending_mfa
    status = sync_runner.get_status(state)
    assert status["state"] == "done"
    assert status["services"]["garmin"] == {"state": "done", "error": None}
    assert status["error"] is None
    # New data in the DB: the user's map is dropped so the next GET rebuilds.
    assert state.data is None


def test_garmin_error_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: UserState
) -> None:
    def fake_sync(ctx: GarminSyncContext) -> None:
        raise SyncError("Garmin rate-limited: wait 15-60 minutes")

    _garmin(monkeypatch, fake_sync)
    data = _warm(state)
    sync_runner.begin(state)
    _run(sync_runner.run_sync(state, _ctx(tmp_path, state)))

    status = sync_runner.get_status(state)
    assert status["state"] == "error"
    assert status["services"]["garmin"]["state"] == "error"
    assert "rate-limited" in status["services"]["garmin"]["error"]
    assert status["needs_mfa"] is False and status["needs_garmin_login"] is False
    # A rebuild re-parses every GPX file; nothing synced, so keep the cache.
    assert state.data is data


def test_unexpected_exception_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: UserState
) -> None:
    """Non-SyncError exceptions should be caught and surfaced with a prefix."""

    def crash(ctx: GarminSyncContext) -> None:
        raise ValueError("boom")

    _garmin(monkeypatch, crash)
    sync_runner.begin(state)
    _run(sync_runner.run_sync(state, _ctx(tmp_path, state)))

    garmin_status = sync_runner.get_status(state)["services"]["garmin"]
    assert garmin_status["state"] == "error"
    assert "unexpected error" in garmin_status["error"]
    assert "boom" in garmin_status["error"]


def test_garmin_login_required_sets_needs_garmin_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: UserState
) -> None:
    def needs_login(ctx: GarminSyncContext) -> None:
        raise GarminLoginRequiredError("Garmin rejected the username or password.")

    _garmin(monkeypatch, needs_login)
    data = _warm(state)
    sync_runner.begin(state)
    _run(sync_runner.run_sync(state, _ctx(tmp_path, state)))

    status = sync_runner.get_status(state)
    assert status["state"] == "error"
    assert status["needs_garmin_login"] is True
    assert status["needs_mfa"] is False
    assert status["services"]["garmin"] == {
        "state": "needs_garmin_login",
        "error": "Garmin rejected the username or password.",
    }
    assert state.data is data

    # The next run clears the flag.
    sync_runner.begin(state)
    assert sync_runner.get_status(state)["needs_garmin_login"] is False


def test_garmin_mfa_required_sets_needs_mfa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: UserState
) -> None:
    def garmin_mfa(ctx: GarminSyncContext) -> None:
        raise MfaRequiredError("code please")

    _garmin(monkeypatch, garmin_mfa)
    sync_runner.begin(state)
    _run(sync_runner.run_sync(state, _ctx(tmp_path, state)))

    status = sync_runner.get_status(state)
    assert status["state"] == "error"
    assert status["needs_mfa"] is True
    assert status["needs_garmin_login"] is False
    assert status["services"]["garmin"]["state"] == "needs_mfa"

    # The next run clears the flag.
    sync_runner.begin(state)
    assert sync_runner.get_status(state)["needs_mfa"] is False


@pytest.mark.parametrize("outcome", ["ok", "login", "error"])
def test_password_is_dropped_after_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: UserState, outcome: str
) -> None:
    seen: list[str | None] = []

    def fake_sync(ctx: GarminSyncContext) -> None:
        seen.append(ctx.password)
        if outcome == "login":
            raise GarminLoginRequiredError("nope")
        if outcome == "error":
            raise RuntimeError("crash")

    _garmin(monkeypatch, fake_sync)
    ctx = _ctx(tmp_path, state, password="garmin-pw")
    sync_runner.begin(state)
    _run(sync_runner.run_sync(state, ctx))

    assert seen == ["garmin-pw"]
    assert ctx.password is None
    assert "garmin-pw" not in repr(ctx)
    assert "garmin-pw" not in repr(sync_runner.get_status(state))


def test_terminal_state_clears_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: UserState
) -> None:
    _garmin(monkeypatch, lambda ctx: None)
    sync_runner.begin(state)
    assert sync_runner.is_running(state) is True
    _run(sync_runner.run_sync(state, _ctx(tmp_path, state)))
    assert sync_runner.is_running(state) is False
    assert sync_runner.get_status(state)["finished_at"] is not None


def test_cancelled_run_does_not_stay_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: UserState
) -> None:
    """A run killed mid-flight (task cancelled on shutdown) must not leave the
    user stuck in "running" — begin() would then 409 forever."""

    def cancelled(ctx: GarminSyncContext) -> None:
        raise asyncio.CancelledError

    _garmin(monkeypatch, cancelled)
    sync_runner.begin(state)
    with pytest.raises(asyncio.CancelledError):
        _run(sync_runner.run_sync(state, _ctx(tmp_path, state)))
    status = sync_runner.get_status(state)
    assert status["state"] == "error"
    assert status["error"] == "sync interrupted"
    assert status["finished_at"] is not None
    assert sync_runner.begin(state) is True


# ── Per-user isolation ───────────────────────────────────────────────


def test_users_sync_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = StateRegistry()
    alice, bob = registry.get(1), registry.get(2)

    # One user's in-flight sync doesn't 409 another's.
    assert sync_runner.begin(alice) is True
    assert sync_runner.begin(bob) is True
    assert sync_runner.begin(alice) is False

    def garmin_mfa(ctx: GarminSyncContext) -> None:
        raise MfaRequiredError("code please")

    _garmin(monkeypatch, garmin_mfa)
    bob_data = _warm(bob)
    _warm(alice)
    _run(sync_runner.run_sync(alice, _ctx(tmp_path, alice)))

    # Alice's MFA prompt and status stay hers; Bob is still mid-run.
    assert sync_runner.get_status(alice)["needs_mfa"] is True
    assert sync_runner.get_status(bob)["needs_mfa"] is False
    assert sync_runner.is_running(bob) is True
    assert bob.data is bob_data

    _garmin(monkeypatch, lambda ctx: None)
    _run(sync_runner.run_sync(bob, _ctx(tmp_path, bob)))
    # Bob's sync invalidates only Bob's map.
    assert bob.data is None
    assert alice.data is not None
