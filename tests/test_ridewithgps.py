"""Ride with GPS link import: URL parsing, JSON -> GPX, and the endpoint.

No network: `ridewithgps._download` is monkeypatched to serve canned JSON
(or raise) and to record which URL it was asked for.
"""

import io
import json
import urllib.request
from http.client import HTTPMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ctx, user_id
from wishmap import gpx, remote_track, ridewithgps

TRIP = {
    "id": 100124261,
    "name": "Alpine Bike 1 <Route>",
    "description": "Eight riding days.",
    "track_points": [
        {"x": 10.295836, "y": 46.795601, "e": 1246.0, "t": 1657604289},
        {"x": 10.2959, "y": 46.7957},
        {"y": 46.8},  # malformed: no lon
        {"x": 10.296, "y": 46.7958, "e": 1250.5},
    ],
}


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve TRIP for every download; return the list of requested URLs."""
    urls: list[str] = []

    def fake(url: str) -> bytes:
        urls.append(url)
        return json.dumps(TRIP).encode()

    monkeypatch.setattr(ridewithgps, "_download", fake)
    return urls


def _fail_with(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def fake(url: str) -> bytes:
        raise exc

    monkeypatch.setattr(ridewithgps, "_download", fake)


def _import(client: TestClient, **overrides: object):  # noqa: ANN202
    body: dict[str, object] = {
        "url": "https://ridewithgps.com/trips/100124261",
        "sport": "biking",
        "status": "done",
        "tags": ["alps"],
    }
    body.update(overrides)
    return client.post("/api/routes/import", json=body)


# ── parse_url ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ridewithgps.com/trips/100124261", ("trip", "100124261")),
        ("https://www.ridewithgps.com/routes/42?privacy_code=x", ("route", "42")),
        ("  http://ridewithgps.com/trips/7/  ", ("trip", "7")),
        ("https://ridewithgps.com/trips/7.gpx", ("trip", "7")),
        ("https://example.com/trips/1", None),
        ("https://ridewithgps.com.evil.com/trips/1", None),
        ("https://ridewithgps.com/users/1", None),
        ("https://ridewithgps.com/trips/abc", None),
        ("https://ridewithgps.com/trips/1abc", None),
        ("ftp://ridewithgps.com/trips/1", None),
        ("not a url", None),
    ],
)
def test_parse_url(url: str, expected: tuple[str, str] | None) -> None:
    assert ridewithgps.parse_url(url) == expected


# ── JSON -> Track -> GPX ─────────────────────────────────────────────


def test_parse_track_skips_bad_points_and_keeps_elevation() -> None:
    track = ridewithgps.parse_track("trip", "1", TRIP)
    assert track.name == "Alpine Bike 1 <Route>"
    assert track.points == [
        [10.295836, 46.795601, 1246.0],
        [10.2959, 46.7957],
        [10.296, 46.7958, 1250.5],
    ]
    assert track.url == "https://ridewithgps.com/trips/1"


def test_parse_track_accepts_wrapped_answer() -> None:
    track = ridewithgps.parse_track("route", "5", {"type": "route", "route": TRIP})
    assert len(track.points) == 3


def test_parse_track_without_points_raises() -> None:
    with pytest.raises(remote_track.RemoteTrackError):
        ridewithgps.parse_track("trip", "1", {"name": "x", "track_points": []})
    with pytest.raises(remote_track.RemoteTrackError):
        ridewithgps.parse_track("trip", "1", ["not", "a", "dict"])


def test_to_gpx_round_trips_and_escapes_name() -> None:
    track = ridewithgps.parse_track("trip", "1", TRIP)
    text = remote_track.to_gpx(track, creator="wishmap & co")
    assert "Alpine Bike 1 &lt;Route&gt;" in text
    assert 'creator="wishmap &amp; co"' in text
    assert gpx.parse_gpx_data(text) == track.coords


def test_fetch_track_only_asks_the_fixed_host(downloads: list[str]) -> None:
    ridewithgps.fetch_track("route", "42")
    assert downloads == ["https://ridewithgps.com/routes/42.json"]
    with pytest.raises(ValueError):
        ridewithgps.fetch_track("trip", "1/../../x")


def test_redirect_off_site_refused() -> None:
    handler = remote_track.SameSiteRedirects(ridewithgps.DOMAIN, ridewithgps.SITE)
    req = urllib.request.Request("https://ridewithgps.com/trips/1.json")

    def redirect(url: str) -> urllib.request.Request | None:
        return handler.redirect_request(
            req, io.BytesIO(), 302, "Found", HTTPMessage(), url
        )

    with pytest.raises(remote_track.RemoteTrackError):
        redirect("https://evil.example/x")
    with pytest.raises(remote_track.RemoteTrackError):
        redirect("http://ridewithgps.com/x")
    with pytest.raises(remote_track.RemoteTrackError):
        redirect("https://ridewithgps.com.evil.example/x")
    assert redirect("https://www.ridewithgps.com/trips/1.json") is not None


# ── POST /api/routes/import ──────────────────────────────────────────


def test_import_creates_route(user_client: TestClient, downloads: list[str]) -> None:
    resp = _import(user_client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] is True
    props = body["route"]["properties"]
    assert props["id"] == "web-rwgps-trip-100124261"
    assert props["name"] == "Alpine Bike 1 <Route>"
    assert props["sport"] == ["biking"]
    assert props["tags"] == ["alps"]
    assert props["notes"] == (
        "https://ridewithgps.com/trips/100124261\n\nEight riding days."
    )
    # Display geometry is rounded; the stored GPX keeps full precision.
    assert body["start"]["geometry"]["coordinates"] == pytest.approx(
        [10.295836, 46.795601], abs=1e-4
    )
    assert downloads == ["https://ridewithgps.com/trips/100124261.json"]

    stored: Path = (
        ctx().user_dir(user_id("alice")) / "gpx" / "web" / "web-rwgps-trip-100124261.gpx"
    )
    assert gpx.parse_gpx(stored)[0] == [10.295836, 46.795601]
    ids = {f["properties"]["id"] for f in user_client.get("/api/routes").json()["features"]}
    assert "web-rwgps-trip-100124261" in ids


def test_import_name_override_and_reimport_replaces(
    user_client: TestClient, downloads: list[str]
) -> None:
    assert _import(user_client).status_code == 201
    resp = _import(user_client, name="  My Alps  ")
    assert resp.status_code == 201
    assert resp.json()["created"] is False
    assert resp.json()["route"]["properties"]["name"] == "My Alps"
    ids = [f["properties"]["id"] for f in user_client.get("/api/routes").json()["features"]]
    assert ids.count("web-rwgps-trip-100124261") == 1


@pytest.mark.parametrize(
    "url", ["https://www.strava.com/routes/1", "https://ridewithgps.com/users/9"]
)
def test_import_rejects_other_links(
    user_client: TestClient, downloads: list[str], url: str
) -> None:
    resp = _import(user_client, url=url)
    assert resp.status_code == 422
    assert downloads == []


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (remote_track.NotPublicError("private"), 422),
        (remote_track.NotFoundError("missing"), 422),
        (remote_track.RemoteTrackError("down"), 502),
    ],
)
def test_import_upstream_errors_write_nothing(
    user_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    status: int,
) -> None:
    _fail_with(monkeypatch, exc)
    resp = _import(user_client)
    assert resp.status_code == status
    assert resp.json()["detail"] == str(exc)
    assert not (ctx().user_dir(user_id("alice")) / "gpx" / "web").exists()


def test_import_invalid_status_422_before_fetch(
    user_client: TestClient, downloads: list[str]
) -> None:
    assert _import(user_client, status="someday").status_code == 422
    assert downloads == []


def test_import_requires_login(client: TestClient, downloads: list[str]) -> None:
    assert _import(client).status_code == 401
    assert downloads == []
