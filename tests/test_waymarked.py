"""Waymarked Trails link import: URL parsing, the route walk, the endpoint.

No network: `waymarked._download` is monkeypatched to serve canned JSON
(or raise) and to record which URL it was asked for.
"""

import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ctx, user_id
from wishmap import gpx, link_import, remote_track, waymarked

R = 6378137.0


def merc(lon: float, lat: float) -> list[float]:
    """[lon, lat] -> web mercator metres, as the API sends geometry."""
    x = math.radians(lon) * R
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R
    return [x, y]


def flat(points: list[list[float]]) -> list[float]:
    return [c for p in points for c in p]


def way(*lon_lats: tuple[float, float]) -> dict[str, object]:
    return {
        "route_type": "base",
        "geometry": {"type": "LineString", "coordinates": [merc(*p) for p in lon_lats]},
    }


# A superroute of two routes: the first has a split stretch (forward is
# followed, backward left out) and an appendix (left out); ways share end
# points, which appear once.
DETAILS = {
    "type": "relation",
    "id": 19216720,
    "ref": "BSR",
    "name": "Bodensee-Radweg <Lindau>",
    "operator": "Arbeitsgemeinschaft Bodensee-Radweg",
    "url": "https://www.bodensee-radweg.de",
    "route": {
        "route_type": "route",
        "main": [
            {
                "route_type": "route",
                "main": [
                    {"route_type": "linear", "ways": [way((9.0, 47.0), (9.1, 47.1)), way((9.1, 47.1), (9.2, 47.2))]},
                    {
                        "route_type": "split",
                        "forward": [{"route_type": "linear", "ways": [way((9.2, 47.2), (9.3, 47.25))]}],
                        "backward": [{"route_type": "linear", "ways": [way((9.3, 47.25), (5.0, 5.0))]}],
                    },
                ],
                "appendices": [
                    {"route_type": "appendix", "main": [{"route_type": "linear", "ways": [way((1.0, 1.0), (2.0, 2.0))]}]}
                ],
            },
            {
                "route_type": "route",
                "main": [
                    {"route_type": "linear", "ways": [way((9.3, 47.25), (9.4, 47.3)), {"geometry": "bad"}]},
                ],
            },
        ],
    },
}
EXPECTED = [[9.0, 47.0], [9.1, 47.1], [9.2, 47.2], [9.3, 47.25], [9.4, 47.3]]
LINK = "https://cycling.waymarkedtrails.org/#route?id=19216720&map=12.0/47.5/9.6"


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve DETAILS for every download; return the list of requested URLs."""
    urls: list[str] = []

    def fake(url: str) -> bytes:
        urls.append(url)
        return json.dumps(DETAILS).encode()

    monkeypatch.setattr(waymarked, "_download", fake)
    return urls


def _import(client: TestClient, **overrides: object):  # noqa: ANN202
    body: dict[str, object] = {"url": LINK, "sport": "biking", "status": "idea", "tags": ["lake"]}
    body.update(overrides)
    return client.post("/api/routes/import", json=body)


# ── parse_url ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (LINK, ("cycling", "19216720")),
        ("https://hiking.waymarkedtrails.org/#route?type=relation&id=7", ("hiking", "7")),
        ("  http://mtb.waymarkedtrails.org/#route?id=3  ", ("mtb", "3")),
        ("https://slopes.waymarkedtrails.org/de/#route?id=3&type=relation", ("slopes", "3")),
        ("https://hiking.waymarkedtrails.org/#route?id=7&type=way", None),
        ("https://hiking.waymarkedtrails.org/#route?id=7&type=wayset", None),
        ("https://hiking.waymarkedtrails.org/#route?id=7a", None),
        ("https://hiking.waymarkedtrails.org/#route?id=1&id=2", None),
        ("https://hiking.waymarkedtrails.org/#search?query=x", None),
        ("https://hiking.waymarkedtrails.org/?id=7", None),
        ("https://evil.waymarkedtrails.org/#route?id=7", None),
        ("https://cycling.waymarkedtrails.org.evil.com/#route?id=7", None),
        ("ftp://cycling.waymarkedtrails.org/#route?id=7", None),
    ],
)
def test_parse_url(url: str, expected: tuple[str, str] | None) -> None:
    assert waymarked.parse_url(url) == expected


# ── JSON -> Track ────────────────────────────────────────────────────


def test_parse_track_walks_main_forward_and_skips_appendices() -> None:
    track = waymarked.parse_track("cycling", "19216720", DETAILS)
    assert flat(track.points) == pytest.approx(flat(EXPECTED), abs=1e-6)
    assert track.name == "Bodensee-Radweg <Lindau>"
    assert track.url == "https://cycling.waymarkedtrails.org/#route?id=19216720&type=relation"
    assert track.description == (
        "Ref: BSR\nOperator: Arbeitsgemeinschaft Bodensee-Radweg\n"
        "Website: https://www.bodensee-radweg.de"
    )


def test_parse_track_falls_back_to_ref_for_the_name() -> None:
    track = waymarked.parse_track("cycling", "1", {**DETAILS, "name": None})
    assert track.name == "BSR"


def test_parse_track_without_points_raises() -> None:
    with pytest.raises(remote_track.RemoteTrackError):
        waymarked.parse_track("cycling", "1", {"route": {"route_type": "route", "main": []}})
    with pytest.raises(remote_track.RemoteTrackError):
        waymarked.parse_track("cycling", "1", ["not", "a", "dict"])


def test_fetch_track_only_asks_the_fixed_host(downloads: list[str]) -> None:
    waymarked.fetch_track("hiking", "42")
    assert downloads == ["https://hiking.waymarkedtrails.org/api/v1/details/relation/42"]
    with pytest.raises(ValueError):
        waymarked.fetch_track("hiking", "1/../../x")
    with pytest.raises(ValueError):
        waymarked.fetch_track("evil", "1")  # type: ignore[arg-type]


# ── POST /api/routes/import ──────────────────────────────────────────


def test_resolve_tags_waymarked_routes() -> None:
    link = link_import.resolve(LINK)
    assert link is not None
    assert (link.route_id, link.tags) == ("web-waymarked-cycling-19216720", ("waymarked",))


def test_import_creates_route_tagged_waymarked(
    user_client: TestClient, downloads: list[str]
) -> None:
    resp = _import(user_client)
    assert resp.status_code == 201, resp.text
    props = resp.json()["route"]["properties"]
    assert props["id"] == "web-waymarked-cycling-19216720"
    assert props["name"] == "Bodensee-Radweg <Lindau>"
    assert props["tags"] == ["lake", "waymarked"]
    assert props["notes"].startswith(
        "https://cycling.waymarkedtrails.org/#route?id=19216720&type=relation\n\nRef: BSR"
    )
    assert downloads == ["https://cycling.waymarkedtrails.org/api/v1/details/relation/19216720"]

    stored: Path = (
        ctx().user_dir(user_id("alice")) / "gpx" / "web" / "web-waymarked-cycling-19216720.gpx"
    )
    assert flat(gpx.parse_gpx(stored)) == pytest.approx(flat(EXPECTED), abs=1e-6)

    # The user may tag it "waymarked" too: no duplicate.
    again = _import(user_client, tags=["waymarked"])
    assert again.json()["created"] is False
    assert again.json()["route"]["properties"]["tags"] == ["waymarked"]


def test_import_upstream_error_writes_nothing(
    user_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(url: str) -> bytes:
        raise remote_track.NotFoundError("missing")

    monkeypatch.setattr(waymarked, "_download", fake)
    resp = _import(user_client)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "missing"
    assert not (ctx().user_dir(user_id("alice")) / "gpx" / "web").exists()
