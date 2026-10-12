"""The map's OSM route overlay lookups: `waymarked` list parsing, the
`osm_lookup` cache and limits, and the /api/osm-routes endpoints.

No network: `waymarked._download` is monkeypatched to serve canned JSON
(or raise) and to record which URLs it was asked for; the lookup's clock
is a fake that tests advance by hand.
"""

import email.message
import io
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ctx
from tests.test_waymarked import DETAILS, EXPECTED
from wishmap import gpx, osm_lookup, remote_track, waymarked
from wishmap.app import app

# A captured `list/by_area` answer (hiking, at Lenzerheide; trimmed to
# three results). "AL1" is a group outside INT/NAT/REG/LOC.
LIST = {
    "bbox": [1063658.0, 5897587.0, 1064258.0, 5898187.0],
    "results": [
        {
            "type": "relation",
            "id": 12945244,
            "group": "AL1",
            "linear": "yes",
            "symbol_description": "gelbe Rhomben",
            "symbol_id": "osmc_LOC_empty_yellow-diamond",
        },
        {
            "type": "relation",
            "id": 17439991,
            "ref": "54",
            "name": "Mittelbünden-Panoramaweg",
            "group": "REG",
            "linear": "yes",
            "symbol_description": "Weisse 54 und schwarzer Text Mittelbünden-Panoramaweg "
            "in grünem Quadrat auf gelbem Wegweiser",
            "itinerary": ["Lenzerheide", "Davos"],
            "symbol_id": "swiss_REG_00350034",
        },
        {
            "type": "relation",
            "id": 7450464,
            "ref": "64",
            "name": "ViaSett",
            "group": "NAT",
            "linear": "yes",
            "symbol_description": "weisse 64 auf grünem Viereck",
            "itinerary": ["Chur", "Chiavenna (I)"],
            "symbol_id": "swiss_REG_00360034",
        },
    ],
}

# A click box at Lenzerheide (a few pixels at zoom ~15).
LON, LAT = 9.5577, 46.7286
D = 0.0003
BBOX = f"{LON - D},{LAT - D},{LON + D},{LAT + D}"


@dataclass
class Upstream:
    """The fake Waymarked API: what it was asked, and how it answers."""

    urls: list[str] = field(default_factory=list)
    # site -> exception to raise for that site's list lookups.
    fail: dict[str, Exception] = field(default_factory=dict)
    details_error: Exception | None = None

    def __call__(self, url: str, timeout: float = remote_track.TIMEOUT_S) -> bytes:
        self.urls.append(url)
        site = url.removeprefix("https://").split(".", 1)[0]
        if "/list/by_area" in url:
            assert timeout == waymarked.LOOKUP_TIMEOUT_S
            if site in self.fail:
                raise self.fail[site]
            return json.dumps(LIST).encode()
        assert "/details/relation/" in url
        if self.details_error is not None:
            raise self.details_error
        return json.dumps(DETAILS).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Upstream:
    fake = Upstream()
    monkeypatch.setattr(waymarked, "_download", fake)
    return fake


@pytest.fixture
def clock(client: TestClient) -> Clock:
    """The running app's lookup on a fake clock (its waits advance it)."""
    c = Clock()
    lookup = ctx().osm_lookup
    lookup.clock = c
    lookup.sleep = c.sleep
    return c


def _at(c: TestClient, sites: str = "hiking", bbox: str = BBOX):  # noqa: ANN202
    return c.get("/api/osm-routes/at", params={"sites": sites, "bbox": bbox})


# ── waymarked: mercator and list parsing ─────────────────────────────


@pytest.mark.parametrize("lon_lat", [(0.0, 0.0), (9.5577, 46.7286), (-122.4, 37.8), (179.9, -60.0)])
def test_to_mercator_round_trips(lon_lat: tuple[float, float]) -> None:
    back = waymarked._lon_lat(*waymarked.to_mercator(*lon_lat))
    assert back == pytest.approx(list(lon_lat), abs=1e-6)


def test_parse_list_reads_the_captured_answer() -> None:
    routes = waymarked.parse_list("hiking", LIST)
    assert [r.id for r in routes] == ["12945244", "17439991", "7450464"]
    unnamed, panorama, _ = routes
    assert unnamed.group == "LOC"  # "AL1" is no known level
    assert (unnamed.name, unnamed.ref, unnamed.itinerary) == ("", "", ())
    assert unnamed.symbol_description == "gelbe Rhomben"
    assert panorama == waymarked.OsmRoute(
        site="hiking",
        id="17439991",
        name="Mittelbünden-Panoramaweg",
        ref="54",
        group="REG",
        itinerary=("Lenzerheide", "Davos"),
        symbol_id="swiss_REG_00350034",
        symbol_description=LIST["results"][1]["symbol_description"],  # type: ignore[index]
    )
    assert panorama.url == "https://hiking.waymarkedtrails.org/#route?id=17439991&type=relation"


def test_parse_list_skips_junk() -> None:
    assert waymarked.parse_list("hiking", ["not", "a", "dict"]) == []
    assert waymarked.parse_list("hiking", {"results": "nope"}) == []
    data = {
        "results": [
            "not a dict",
            {"type": "way", "id": 1},
            {"type": "relation", "id": True},
            {"type": "relation", "id": "12a"},
            {"type": "relation", "id": -5},
            {"type": "relation", "id": 1.5},
            {"type": "relation", "id": "1" * 21},
            {
                "type": "relation",
                "id": "42",
                "name": 123,
                "ref": ["x"],
                "group": "XYZ",
                "itinerary": ["  A ", 7, "", "B" * 300],
                "symbol_id": "../../evil.svg",
                "symbol_description": None,
            },
            {"type": "relation", "id": 43, "name": "N" * 500, "group": "INT", "itinerary": "A"},
        ]
    }
    first, second = waymarked.parse_list("cycling", data)
    assert first == waymarked.OsmRoute(
        site="cycling",
        id="42",
        name="",
        ref="",
        group="LOC",
        itinerary=("A", "B" * 100),
        symbol_id=None,
        symbol_description="",
    )
    assert (second.id, len(second.name), second.group, second.itinerary) == ("43", 200, "INT", ())


def test_snap_bbox_shares_the_grid_between_neighbouring_clicks() -> None:
    a = waymarked.snap_bbox(LON - D, LAT - D, LON + D, LAT + D)
    b = waymarked.snap_bbox(LON - D + 1e-5, LAT - D + 1e-5, LON + D + 1e-5, LAT + D + 1e-5)
    assert a == b
    x, y = waymarked.to_mercator(LON, LAT)
    assert a[0] < x < a[2] and a[1] < y < a[3]


# ── remote_track: the request itself ─────────────────────────────────


class _Opener:
    def __init__(self, answer: Callable[[urllib.request.Request], bytes]) -> None:
        self.answer = answer
        self.requests: list[tuple[urllib.request.Request, float]] = []

    def open(self, req: urllib.request.Request, timeout: float) -> io.BytesIO:
        self.requests.append((req, timeout))
        return io.BytesIO(self.answer(req))


def _http_error(code: int, retry_after: str | None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", code, "busy", headers, None)


def _download_with(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[urllib.request.Request], bytes]
) -> _Opener:
    opener = _Opener(answer)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    return opener


def test_download_sends_the_user_agent_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _download_with(monkeypatch, lambda req: b"{}")
    assert waymarked._download("https://hiking.waymarkedtrails.org/api/x", timeout=5) == b"{}"
    ((req, timeout),) = opener.requests
    assert req.get_header("User-agent") == remote_track.USER_AGENT
    assert remote_track.USER_AGENT.startswith("wishmap/")
    assert "github.com/Jimmy2027/wishmap" in remote_track.USER_AGENT
    assert timeout == 5


@pytest.mark.parametrize(
    ("code", "header", "expected"),
    [(429, "120", 120.0), (503, None, 60.0), (429, "Wed, 21 Oct 2015 07:28:00 GMT", 60.0),
     (429, "99999", 900.0), (429, "-3", 60.0)],
)
def test_download_turns_429_and_503_into_rate_limited(
    monkeypatch: pytest.MonkeyPatch, code: int, header: str | None, expected: float
) -> None:
    def answer(req: urllib.request.Request) -> bytes:
        raise _http_error(code, header)

    _download_with(monkeypatch, answer)
    with pytest.raises(remote_track.RateLimitedError) as e:
        waymarked._download("https://hiking.waymarkedtrails.org/api/x")
    assert e.value.retry_after == expected


# ── GET /api/osm-routes/at ───────────────────────────────────────────


def test_at_needs_a_session(client: TestClient, user_client: TestClient, upstream: Upstream) -> None:
    assert _at(client).status_code == 401
    token = user_client.post("/api/tokens", json={"name": "s", "scope": "read"}).json()["token"]
    with TestClient(app) as bare:
        resp = bare.get(
            "/api/osm-routes/at",
            params={"sites": "hiking", "bbox": BBOX},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "session_required"
    assert upstream.urls == []


@pytest.mark.parametrize(
    ("sites", "bbox"),
    [
        ("walking", BBOX),
        ("hiking,evil", BBOX),
        ("", BBOX),
        ("hiking", "1,2,3"),
        ("hiking", "a,b,c,d"),
        ("hiking", "nan,46,9.6,46.1"),
        ("hiking", "9.6,46,9.5,46.1"),  # west > east
        ("hiking", "9.5,46.1,9.6,46"),  # south > north
        ("hiking", "9.5,46,9.5,46.1"),  # empty
        ("hiking", "190,46,191,46.1"),
        ("hiking", "9.5,86,9.6,86.1"),
        ("hiking", "9,46,9.6,46.1"),  # wider than 0.5°
    ],
)
def test_at_rejects_bad_input(
    user_client: TestClient, upstream: Upstream, sites: str, bbox: str
) -> None:
    assert _at(user_client, sites, bbox).status_code == 422
    assert upstream.urls == []


def test_at_lists_routes_widest_network_first(
    user_client: TestClient, upstream: Upstream, clock: Clock
) -> None:
    resp = _at(user_client, "hiking,hiking")  # deduped
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["errors"] == {}
    assert [(r["id"], r["group"]) for r in body["results"]] == [
        ("7450464", "NAT"),
        ("17439991", "REG"),
        ("12945244", "LOC"),
    ]
    assert body["results"][1] == {
        "site": "hiking",
        "id": "17439991",
        "name": "Mittelbünden-Panoramaweg",
        "ref": "54",
        "group": "REG",
        "itinerary": ["Lenzerheide", "Davos"],
        "symbol_id": "swiss_REG_00350034",
        "symbol_description": LIST["results"][1]["symbol_description"],  # type: ignore[index]
        "url": "https://hiking.waymarkedtrails.org/#route?id=17439991&type=relation",
        "adopted": False,
    }

    # One request, in mercator metres, around the click.
    (url,) = upstream.urls
    prefix = "https://hiking.waymarkedtrails.org/api/v1/list/by_area?bbox="
    assert url.startswith(prefix) and url.endswith("&limit=20")
    west, south, east, north = map(float, url.removeprefix(prefix).split("&")[0].split(","))
    x, y = waymarked.to_mercator(LON, LAT)
    assert west < x < east and south < y < north
    assert east - west < 500 and north - south < 500


def test_at_is_cached_and_shared_by_neighbouring_clicks(
    user_client: TestClient, upstream: Upstream, clock: Clock
) -> None:
    assert _at(user_client).status_code == 200
    near = f"{LON - D + 1e-5},{LAT - D},{LON + D + 1e-5},{LAT + D}"
    assert _at(user_client, bbox=near).status_code == 200
    assert len(upstream.urls) == 1
    # The list cache lasts an hour.
    clock.now += 3601
    assert _at(user_client).status_code == 200
    assert len(upstream.urls) == 2


def test_at_marks_routes_already_imported(
    user_client: TestClient, upstream: Upstream, clock: Clock
) -> None:
    resp = user_client.post(
        "/api/routes/import",
        json={
            "url": "https://hiking.waymarkedtrails.org/#route?id=17439991",
            "sport": "hiking",
            "status": "idea",
            "tags": [],
        },
    )
    assert resp.status_code == 201, resp.text
    adopted = {r["id"]: r["adopted"] for r in _at(user_client).json()["results"]}
    assert adopted == {"7450464": False, "17439991": True, "12945244": False}


def test_at_other_users_imports_dont_count(
    user_client: TestClient, second_user_client: TestClient, upstream: Upstream, clock: Clock
) -> None:
    second_user_client.post(
        "/api/routes/import",
        json={"url": "https://hiking.waymarkedtrails.org/#route?id=17439991",
              "sport": "hiking", "status": "idea", "tags": []},
    )
    assert not any(r["adopted"] for r in _at(user_client).json()["results"])


def test_at_per_user_limit(
    user_client: TestClient, second_user_client: TestClient, upstream: Upstream, clock: Clock
) -> None:
    ctx().osm_lookup.user_limit = 3
    for _ in range(3):
        assert _at(user_client).status_code == 200
    resp = _at(user_client)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    # Per user: bob still may.
    assert _at(second_user_client).status_code == 200
    clock.now += 61
    assert _at(user_client).status_code == 200


def test_at_backs_off_when_upstream_says_so(
    user_client: TestClient, upstream: Upstream, clock: Clock
) -> None:
    upstream.fail["hiking"] = remote_track.RateLimitedError("busy", 120)
    resp = _at(user_client)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "120"
    assert len(upstream.urls) == 1

    # Every upstream call pauses: another site, another spot, a details fetch.
    del upstream.fail["hiking"]
    assert _at(user_client, "cycling").status_code == 503
    other = f"{LON + 0.1 - D},{LAT - D},{LON + 0.1 + D},{LAT + D}"
    assert _at(user_client, bbox=other).status_code == 503
    assert user_client.get("/api/osm-routes/hiking/42").status_code == 503
    assert len(upstream.urls) == 1

    clock.now += 121
    assert _at(user_client).status_code == 200
    assert len(upstream.urls) == 2


def test_at_still_serves_cached_hits_while_paused(
    user_client: TestClient, upstream: Upstream, clock: Clock
) -> None:
    assert _at(user_client).status_code == 200
    upstream.fail["cycling"] = remote_track.RateLimitedError("busy", 120)
    assert _at(user_client, "cycling").status_code == 503
    assert _at(user_client).status_code == 200
    assert len(upstream.urls) == 2


def test_at_partial_failure_keeps_the_other_sites(
    user_client: TestClient, upstream: Upstream, clock: Clock
) -> None:
    upstream.fail["cycling"] = remote_track.RemoteTrackError("Could not reach Waymarked Trails.")
    resp = _at(user_client, "cycling,hiking")
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == {"cycling": "Could not reach Waymarked Trails."}
    assert {r["site"] for r in body["results"]} == {"hiking"}
    assert len(body["results"]) == 3

    # Every site failing fails the request.
    upstream.fail["hiking"] = remote_track.RemoteTrackError("nope")
    clock.now += 3601
    assert _at(user_client, "cycling,hiking").status_code == 502


# ── GET /api/osm-routes/{site}/{id} ──────────────────────────────────


def test_details(user_client: TestClient, upstream: Upstream, clock: Clock) -> None:
    resp = user_client.get("/api/osm-routes/cycling/19216720")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    geometry = body.pop("geometry")
    assert body == {
        "site": "cycling",
        "id": "19216720",
        "name": "Bodensee-Radweg <Lindau>",
        "ref": "BSR",
        "length_km": round(gpx.length_m(EXPECTED) / 1000, 1),
        "operator": "Arbeitsgemeinschaft Bodensee-Radweg",
        "website": "https://www.bodensee-radweg.de",
        "description": "",
        "url": "https://cycling.waymarkedtrails.org/#route?id=19216720&type=relation",
    }
    assert body["length_km"] > 0
    assert geometry == {
        "type": "MultiLineString",
        "coordinates": gpx.track_parts(EXPECTED),
    }
    assert upstream.urls == ["https://cycling.waymarkedtrails.org/api/v1/details/relation/19216720"]

    # Cached for a day.
    assert user_client.get("/api/osm-routes/cycling/19216720").status_code == 200
    assert len(upstream.urls) == 1
    clock.now += 24 * 3600 + 1
    assert user_client.get("/api/osm-routes/cycling/19216720").status_code == 200
    assert len(upstream.urls) == 2


@pytest.mark.parametrize(
    "path",
    ["/api/osm-routes/cycling/12a", "/api/osm-routes/evil/1", "/api/osm-routes/hiking,cycling/1",
     "/api/osm-routes/cycling/" + "1" * 21, "/api/osm-routes/cycling/١٢"],
)
def test_details_rejects_bad_input(user_client: TestClient, upstream: Upstream, path: str) -> None:
    assert user_client.get(path).status_code == 422
    assert upstream.urls == []


def test_details_needs_a_session(client: TestClient, upstream: Upstream) -> None:
    assert client.get("/api/osm-routes/cycling/1").status_code == 401


def test_details_upstream_errors(user_client: TestClient, upstream: Upstream, clock: Clock) -> None:
    upstream.details_error = remote_track.NotFoundError("Waymarked Trails has no such route.")
    resp = user_client.get("/api/osm-routes/cycling/1")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Waymarked Trails has no such route."
    upstream.details_error = remote_track.RemoteTrackError("Could not reach Waymarked Trails.")
    assert user_client.get("/api/osm-routes/cycling/1").status_code == 502
    # Errors aren't cached.
    upstream.details_error = None
    assert user_client.get("/api/osm-routes/cycling/1").status_code == 200


# ── OsmLookup: the token bucket and coalescing ───────────────────────


@pytest.fixture
def lookup(upstream: Upstream) -> Iterator[tuple[osm_lookup.OsmLookup, Clock]]:
    lk = osm_lookup.OsmLookup()
    c = Clock()
    lk.clock = c
    lk.sleep = c.sleep
    yield lk, c


def _box(i: int) -> waymarked.Bbox:
    return (i * 1000.0, 0.0, i * 1000.0 + 100, 100.0)


def test_token_bucket_waits_briefly_then_refuses(
    lookup: tuple[osm_lookup.OsmLookup, Clock], upstream: Upstream
) -> None:
    lk, c = lookup
    start = c.now
    for i in range(5):  # the burst
        lk.routes_at("hiking", _box(i))
    assert c.now == start
    lk.routes_at("hiking", _box(5))  # waits half a second for a token
    assert c.now == pytest.approx(start + 0.5)

    lk.sleep = lambda s: None  # time stands still: tokens run out
    for i in range(6, 10):
        lk.routes_at("hiking", _box(i))
    with pytest.raises(osm_lookup.LookupRefused) as e:
        lk.routes_at("hiking", _box(10))
    assert e.value.status == 429
    assert len(upstream.urls) == 10


def test_concurrent_identical_lookups_share_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lk = osm_lookup.OsmLookup()
    entered = threading.Event()
    release = threading.Event()
    urls: list[str] = []

    def slow(url: str, timeout: float = 60) -> bytes:
        urls.append(url)
        entered.set()
        assert release.wait(5)
        return json.dumps(LIST).encode()

    monkeypatch.setattr(waymarked, "_download", slow)
    results: list[list[waymarked.OsmRoute]] = []
    threads = [threading.Thread(target=lambda: results.append(lk.routes_at("hiking", _box(0))))
               for _ in range(3)]
    threads[0].start()
    assert entered.wait(5)
    for t in threads[1:]:
        t.start()
    time.sleep(0.1)  # let the followers reach the in-flight call
    release.set()
    for t in threads:
        t.join(5)
    assert len(urls) == 1
    assert len(results) == 3 and results[0] == results[1] == results[2]
