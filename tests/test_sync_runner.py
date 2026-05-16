"""Unit tests for the sync_runner state machine and error capture.

These tests monkeypatch the actual sync functions (`strava.sync`,
`garmin.sync`) and `app.reload_data` so nothing touches the network or
the lifespan globals. The goal here is to verify the runner's bookkeeping:
state transitions, per-service error capture, skip behavior, and lock
semantics.

Async tests use `asyncio.run` directly rather than pytest-asyncio so this
test module needs no extra dependency.
"""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from wishmap import sync_runner
from wishmap.exceptions import SyncError
from wishmap.models import GarminConfig, StravaConfig, WishmapConfig


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset module state before every test and stub out reload_data.

    reload_data lives on the app module and depends on lifespan-initialized
    globals (_config_path, _base_path). The sync_runner tests don't care
    about reload — that's covered by the endpoint integration tests.
    """
    sync_runner.reset_status_for_tests()

    import wishmap.app

    monkeypatch.setattr(wishmap.app, "reload_data", lambda: None)


def _strava_cfg() -> StravaConfig:
    return StravaConfig(client_id="test", client_secret="secret")


def _garmin_cfg() -> GarminConfig:
    return GarminConfig(username="test", password="pw")


def test_initial_status_is_idle() -> None:
    status = sync_runner.get_status()
    assert status["state"] == "idle"
    assert status["started_at"] is None
    assert status["finished_at"] is None
    assert status["error"] is None
    assert status["services"]["strava"]["state"] == "idle"
    assert status["services"]["garmin"]["state"] == "idle"


def test_is_running_reflects_state() -> None:
    assert sync_runner.is_running() is False
    sync_runner.begin()
    assert sync_runner.is_running() is True


def test_begin_returns_false_when_already_running() -> None:
    assert sync_runner.begin() is True
    assert sync_runner.begin() is False


def test_begin_resets_started_at_and_clears_per_service() -> None:
    sync_runner.begin()
    s = sync_runner.get_status()
    assert s["started_at"] is not None
    assert s["error"] is None
    assert all(svc["state"] == "idle" for svc in s["services"].values())


def test_run_sync_with_no_services_marks_skipped_and_done(tmp_path: Path) -> None:
    cfg = WishmapConfig()  # both garmin and strava are None
    sync_runner.begin()
    _run(sync_runner.run_sync(cfg, tmp_path))
    status = sync_runner.get_status()
    assert status["state"] == "done"
    assert status["services"]["strava"]["state"] == "skipped"
    assert status["services"]["garmin"]["state"] == "skipped"
    assert status["finished_at"] is not None


def test_run_sync_both_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_strava_sync(cfg: Any, base_path: Path) -> None:
        calls.append("strava")

    def fake_garmin_sync(
        cfg: Any, base_path: Path, strava_db: Path | None = None
    ) -> None:
        calls.append("garmin")

    monkeypatch.setattr("wishmap.strava.sync", fake_strava_sync)
    monkeypatch.setattr("wishmap.garmin.sync", fake_garmin_sync)

    cfg = WishmapConfig(strava=_strava_cfg(), garmin=_garmin_cfg())
    sync_runner.begin()
    _run(sync_runner.run_sync(cfg, tmp_path))

    # Order matters: Strava first so its DB exists when Garmin matches.
    assert calls == ["strava", "garmin"]
    status = sync_runner.get_status()
    assert status["state"] == "done"
    assert status["services"]["strava"]["state"] == "done"
    assert status["services"]["garmin"]["state"] == "done"
    assert status["error"] is None


def test_strava_error_does_not_block_garmin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_strava_sync(cfg: Any, base_path: Path) -> None:
        raise SyncError("token expired")

    garmin_ran = False

    def fake_garmin_sync(cfg: Any, base_path: Path, strava_db: Any = None) -> None:
        nonlocal garmin_ran
        garmin_ran = True

    monkeypatch.setattr("wishmap.strava.sync", fake_strava_sync)
    monkeypatch.setattr("wishmap.garmin.sync", fake_garmin_sync)

    cfg = WishmapConfig(strava=_strava_cfg(), garmin=_garmin_cfg())
    sync_runner.begin()
    _run(sync_runner.run_sync(cfg, tmp_path))

    assert garmin_ran is True
    status = sync_runner.get_status()
    assert status["state"] == "error"
    assert status["services"]["strava"]["state"] == "error"
    assert status["services"]["strava"]["error"] == "token expired"
    assert status["services"]["garmin"]["state"] == "done"


def test_garmin_error_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("wishmap.strava.sync", lambda c, b: None)

    def fake_garmin_sync(cfg: Any, base_path: Path, strava_db: Any = None) -> None:
        raise SyncError("Garmin rate-limited: wait 15-60 minutes")

    monkeypatch.setattr("wishmap.garmin.sync", fake_garmin_sync)

    cfg = WishmapConfig(strava=_strava_cfg(), garmin=_garmin_cfg())
    sync_runner.begin()
    _run(sync_runner.run_sync(cfg, tmp_path))

    status = sync_runner.get_status()
    assert status["state"] == "error"
    assert status["services"]["strava"]["state"] == "done"
    assert status["services"]["garmin"]["state"] == "error"
    assert "rate-limited" in status["services"]["garmin"]["error"]


def test_both_services_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise SyncError("nope")

    monkeypatch.setattr("wishmap.strava.sync", boom)
    monkeypatch.setattr("wishmap.garmin.sync", boom)

    cfg = WishmapConfig(strava=_strava_cfg(), garmin=_garmin_cfg())
    sync_runner.begin()
    _run(sync_runner.run_sync(cfg, tmp_path))

    status = sync_runner.get_status()
    assert status["state"] == "error"
    assert status["services"]["strava"]["state"] == "error"
    assert status["services"]["garmin"]["state"] == "error"


def test_unexpected_exception_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-SyncError exceptions should be caught and surfaced with a prefix."""

    def crash(cfg: Any, base_path: Path) -> None:
        raise ValueError("boom")

    monkeypatch.setattr("wishmap.strava.sync", crash)

    cfg = WishmapConfig(strava=_strava_cfg())
    sync_runner.begin()
    _run(sync_runner.run_sync(cfg, tmp_path))

    status = sync_runner.get_status()
    assert status["services"]["strava"]["state"] == "error"
    assert "unexpected error" in status["services"]["strava"]["error"]
    assert "boom" in status["services"]["strava"]["error"]


def test_reload_data_failure_surfaces_top_level_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("wishmap.strava.sync", lambda c, b: None)
    monkeypatch.setattr("wishmap.garmin.sync", lambda c, b, sd: None)

    import wishmap.app

    def broken_reload() -> None:
        raise SyncError("config reload failed: missing GPX")

    monkeypatch.setattr(wishmap.app, "reload_data", broken_reload)

    cfg = WishmapConfig(strava=_strava_cfg(), garmin=_garmin_cfg())
    sync_runner.begin()
    _run(sync_runner.run_sync(cfg, tmp_path))

    status = sync_runner.get_status()
    assert status["state"] == "error"
    assert status["services"]["strava"]["state"] == "done"
    assert status["services"]["garmin"]["state"] == "done"
    assert "missing GPX" in status["error"]


def test_terminal_state_clears_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("wishmap.strava.sync", lambda c, b: None)

    cfg = WishmapConfig(strava=_strava_cfg())
    sync_runner.begin()
    assert sync_runner.is_running() is True
    _run(sync_runner.run_sync(cfg, tmp_path))
    assert sync_runner.is_running() is False
    assert sync_runner.get_status()["finished_at"] is not None
