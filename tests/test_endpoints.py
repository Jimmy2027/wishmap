"""HTTP integration tests for the ratings endpoints.

Build a self-contained wishmap install in a tmp_path (config TOML + one
minimal GPX), point WISHMAP_CONFIG at it, and drive FastAPI's TestClient
which runs the lifespan startup that wires up `_db_path`.
"""

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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

    # Reload so any prior test's lifespan globals don't bleed in.
    import wishmap.app as app_module
    importlib.reload(app_module)

    with TestClient(app_module.app) as c:
        yield c


def test_get_ratings_empty(client: TestClient) -> None:
    resp = client.get("/api/ratings")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_put_unknown_route_id_is_404(client: TestClient) -> None:
    resp = client.put("/api/ratings/nope", json={"fun": 3})
    assert resp.status_code == 404


def test_put_known_route_returns_rating(client: TestClient) -> None:
    resp = client.put("/api/ratings/r1", json={"fun": 4})
    assert resp.status_code == 200
    body = resp.json()
    assert body["fun"] == 4
    assert body["difficulty"] is None
    assert body["scenery"] is None
    assert body["updated_at"]


def test_get_after_put(client: TestClient) -> None:
    client.put("/api/ratings/r1", json={"fun": 4, "scenery": 5})
    resp = client.get("/api/ratings")
    assert resp.status_code == 200
    body = resp.json()
    assert "r1" in body
    assert body["r1"]["fun"] == 4
    assert body["r1"]["scenery"] == 5
    assert body["r1"]["difficulty"] is None


def test_put_null_clears_axis(client: TestClient) -> None:
    client.put("/api/ratings/r1", json={"fun": 4, "scenery": 5})
    resp = client.put("/api/ratings/r1", json={"fun": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["fun"] is None
    assert body["scenery"] == 5


def test_put_absent_axis_preserved(client: TestClient) -> None:
    client.put("/api/ratings/r1", json={"fun": 4, "scenery": 5})
    resp = client.put("/api/ratings/r1", json={"difficulty": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["fun"] == 4
    assert body["difficulty"] == 2
    assert body["scenery"] == 5


def test_put_empty_body_refreshes_updated_at(client: TestClient) -> None:
    first = client.put("/api/ratings/r1", json={"fun": 4}).json()
    second = client.put("/api/ratings/r1", json={}).json()
    assert second["fun"] == 4
    assert second["updated_at"] >= first["updated_at"]


@pytest.mark.parametrize("bad", [0, 6, -1, 100])
def test_put_out_of_range_is_422(client: TestClient, bad: int) -> None:
    resp = client.put("/api/ratings/r1", json={"fun": bad})
    assert resp.status_code == 422
