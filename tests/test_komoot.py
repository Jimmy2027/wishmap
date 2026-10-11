"""Komoot link import: URL parsing, JSON -> Track, the endpoint, and the
link dispatcher picking the right platform.

No network: `komoot._download` (and `ridewithgps._download`) are
monkeypatched to serve canned JSON (or raise) and to record which URLs
they were asked for.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ctx, user_id
from wishmap import gpx, komoot, link_import, remote_track, ridewithgps

TOUR = {
    "id": 23525904,
    "name": "Lucca City Walls <Loop>",
    "sport": "mtb_easy",
    "status": "PUBLIC",
}
COORDINATES = {
    "items": [
        {"lat": 43.707841, "lng": 10.398411, "alt": 4.3, "t": 0},
        {"lat": 43.707862, "lng": 10.398297, "t": 10551},
        {"lat": 43.71},  # malformed: no lng
        {"lat": 91.0, "lng": 10.0},  # out of range
        {"lat": 43.707999, "lng": 10.398346, "alt": 4.4, "t": 20355},
    ]
}
SMARTTOUR_LINK = (
    "https://www.komoot.com/smarttour/23525904?ref=itd&query=d05A%3DFyj4"
    "&t_s=referral&t_cid=route_share&t_ref_username=473412483742"
)


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve TOUR / COORDINATES; return the list of requested URLs."""
    urls: list[str] = []

    def fake(url: str) -> bytes:
        urls.append(url)
        coords = url.split("?")[0].endswith("/coordinates")
        return json.dumps(COORDINATES if coords else TOUR).encode()

    monkeypatch.setattr(komoot, "_download", fake)
    return urls


def _import(client: TestClient, **overrides: object):  # noqa: ANN202
    body: dict[str, object] = {
        "url": SMARTTOUR_LINK,
        "sport": "biking",
        "status": "idea",
    }
    body.update(overrides)
    return client.post("/api/routes/import", json=body)


# ── parse_url ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SMARTTOUR_LINK, ("smarttour", "23525904", None)),
        ("https://www.komoot.com/tour/123", ("tour", "123", None)),
        ("  http://komoot.de/tour/7/  ", ("tour", "7", None)),
        ("https://www.komoot.de/de-de/tour/1?share_token=aB-9_x", ("tour", "1", "aB-9_x")),
        ("https://www.komoot.com/en/tour/1/zoom", ("tour", "1", None)),
        # A token with odd characters is dropped, not passed on.
        ("https://www.komoot.com/tour/1?share_token=a%2Fb", ("tour", "1", None)),
        ("https://komoot.com.evil.com/tour/1", None),
        ("https://www.komoot.com/user/1", None),
        ("https://www.komoot.com/tour/abc", None),
        ("https://www.komoot.com/tour/1abc", None),
        ("ftp://www.komoot.com/tour/1", None),
        ("https://ridewithgps.com/trips/1", None),
    ],
)
def test_parse_url(url: str, expected: tuple[str, str, str | None] | None) -> None:
    assert komoot.parse_url(url) == expected


# ── JSON -> Track ────────────────────────────────────────────────────


def test_parse_track_skips_bad_points_and_keeps_elevation() -> None:
    track = komoot.parse_track("tour", "1", TOUR, COORDINATES)
    assert track.name == "Lucca City Walls <Loop>"
    assert track.points == [
        [10.398411, 43.707841, 4.3],
        [10.398297, 43.707862],
        [10.398346, 43.707999, 4.4],
    ]
    assert track.url == "https://www.komoot.com/tour/1"


def test_parse_track_without_points_raises() -> None:
    with pytest.raises(remote_track.RemoteTrackError):
        komoot.parse_track("tour", "1", TOUR, {"items": []})
    with pytest.raises(remote_track.RemoteTrackError):
        komoot.parse_track("tour", "1", ["not", "a", "dict"], COORDINATES)


def test_fetch_track_only_asks_the_fixed_host(downloads: list[str]) -> None:
    komoot.fetch_track("smarttour", "9")
    komoot.fetch_track("tour", "42", "tok")
    assert downloads == [
        "https://api.komoot.de/v007/smart_tours/9",
        "https://api.komoot.de/v007/smart_tours/9/coordinates",
        "https://api.komoot.de/v007/tours/42?share_token=tok",
        "https://api.komoot.de/v007/tours/42/coordinates?share_token=tok",
    ]
    with pytest.raises(ValueError):
        komoot.fetch_track("tour", "1/../../x")
    with pytest.raises(ValueError):
        komoot.fetch_track("tour", "1", "a&b=c")


def test_download_refuses_other_hosts() -> None:
    with pytest.raises(ValueError):
        komoot._download("https://evil.example/v007/tours/1")


# ── link_import.resolve ──────────────────────────────────────────────


def test_resolve_picks_the_platform() -> None:
    rwgps = link_import.resolve("https://ridewithgps.com/routes/42")
    assert rwgps is not None
    assert (rwgps.site, rwgps.route_id) == ("Ride with GPS", "web-rwgps-route-42")
    km = link_import.resolve(SMARTTOUR_LINK)
    assert km is not None
    assert (km.site, km.route_id) == ("Komoot", "web-komoot-smarttour-23525904")
    assert link_import.resolve("https://www.strava.com/routes/1") is None


# ── POST /api/routes/import ──────────────────────────────────────────


def test_import_komoot_creates_route(user_client: TestClient, downloads: list[str]) -> None:
    resp = _import(user_client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] is True
    props = body["route"]["properties"]
    assert props["id"] == "web-komoot-smarttour-23525904"
    assert props["name"] == "Lucca City Walls <Loop>"
    assert props["notes"] == "https://www.komoot.com/smarttour/23525904"
    assert downloads == [
        "https://api.komoot.de/v007/smart_tours/23525904",
        "https://api.komoot.de/v007/smart_tours/23525904/coordinates",
    ]
    stored: Path = (
        ctx().user_dir(user_id("alice"))
        / "gpx"
        / "web"
        / "web-komoot-smarttour-23525904.gpx"
    )
    assert gpx.parse_gpx(stored)[0] == [10.398411, 43.707841]
    assert 'creator="wishmap (Komoot import)"' in stored.read_text()

    again = _import(user_client, name="Lucca")
    assert again.json()["created"] is False
    assert again.json()["route"]["properties"]["name"] == "Lucca"


def test_import_dispatches_by_link(
    user_client: TestClient, downloads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    rwgps_urls: list[str] = []

    def fake_rwgps(url: str) -> bytes:
        rwgps_urls.append(url)
        return json.dumps(
            {"name": "R", "track_points": [{"x": 10.0, "y": 46.0}, {"x": 10.1, "y": 46.1}]}
        ).encode()

    monkeypatch.setattr(ridewithgps, "_download", fake_rwgps)
    resp = _import(user_client, url="https://ridewithgps.com/routes/5")
    assert resp.status_code == 201, resp.text
    assert resp.json()["route"]["properties"]["id"] == "web-rwgps-route-5"
    assert rwgps_urls == ["https://ridewithgps.com/routes/5.json"]
    assert downloads == []


def test_import_unknown_link_names_supported_platforms(
    user_client: TestClient, downloads: list[str]
) -> None:
    resp = _import(user_client, url="https://www.strava.com/routes/1")
    assert resp.status_code == 422
    assert "Komoot" in resp.json()["detail"]
    assert "Ride with GPS" in resp.json()["detail"]
    assert downloads == []


def test_import_private_komoot_tour_422(
    user_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(url: str) -> bytes:
        raise remote_track.NotPublicError("private")

    monkeypatch.setattr(komoot, "_download", fake)
    resp = _import(user_client, url="https://www.komoot.com/tour/1")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "private"
    assert not (ctx().user_dir(user_id("alice")) / "gpx" / "web").exists()
