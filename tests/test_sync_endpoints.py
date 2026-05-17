"""HTTP integration tests for the /api/sync endpoints.

These tests mock out `sync_runner.run_sync` so no actual sync runs — the
focus is on the endpoint contract (202 vs 409, response shape, idempotent
spam handling). Per-service error capture is covered by test_sync_runner.py.

Mirrors the test_endpoints.py pattern: build a minimal wishmap install in
tmp_path, point WISHMAP_CONFIG at it, reload the app module so lifespan
re-runs.
"""

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wishmap import sync_runner

MINIMAL_GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>t</name><trkseg>
    <trkpt lat="46.9" lon="9.7"/>
    <trkpt lat="46.91" lon="9.71"/>
  </trkseg></trk>
</gpx>
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    gpx = tmp_path / "route.gpx"
    gpx.write_text(MINIMAL_GPX)

    (tmp_path / "wishmap.toml").write_text(
        'title = "test"\n'
        "\n"
        "[[routes]]\n"
        'id = "r1"\n'
        'name = "Route One"\n'
        'sport = "hiking"\n'
        'status = "planned"\n'
        'gpx = "route.gpx"\n'
    )

    monkeypatch.setenv("WISHMAP_CONFIG", str(tmp_path / "wishmap.toml"))

    # Replace run_sync with a no-op coroutine so endpoint tests don't kick
    # off a real Garmin/Strava sync. State is still flipped by begin()
    # synchronously inside the POST handler, so 202/409 semantics work.
    async def noop_run_sync(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(sync_runner, "run_sync", noop_run_sync)
    sync_runner.reset_status_for_tests()

    # Reload so any prior test's lifespan globals don't bleed in.
    import wishmap.app as app_module
    importlib.reload(app_module)

    with TestClient(app_module.app) as c:
        yield c


def test_initial_status_is_idle(client: TestClient) -> None:
    resp = client.get("/api/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "idle"
    assert body["started_at"] is None
    assert body["finished_at"] is None
    assert body["error"] is None
    assert set(body["services"].keys()) == {"strava", "garmin"}


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

    second = client.post("/api/sync")
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
        "needs_passphrase",
    }
    assert body["needs_passphrase"] is False
    for svc in ("strava", "garmin"):
        assert set(body["services"][svc].keys()) == {"state", "error"}


def test_status_endpoint_reflects_post(client: TestClient) -> None:
    before = client.get("/api/sync/status").json()
    assert before["state"] == "idle"

    client.post("/api/sync")

    after = client.get("/api/sync/status").json()
    assert after["state"] == "running"
    assert after["started_at"] is not None
