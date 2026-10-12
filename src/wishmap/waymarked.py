"""Import a Waymarked Trails route (an OpenStreetMap route relation) from its link.

Waymarked Trails (`<site>.waymarkedtrails.org`, one site per activity:
cycling, mtb, hiking, ...) shows OSM's signposted routes. Its links keep
the route in the fragment (`#route?id=<relation id>&type=relation`), and
the JSON its own map uses, `/api/v1/details/relation/<id>`, has the name,
tags and the whole route: `route` nests `route` (a superroute's parts,
or the route itself: `main` in order, `appendices` aside), `linear` (its
`ways`, each oriented in travel direction) and `split` (`forward` and
`backward` branches of a stretch ridden differently each way). Way
geometry is in web mercator metres (EPSG:3857). The data is OSM's (ODbL),
public and needs no login; the API is undocumented, so everything here is
defensive.

The request always goes to the fixed host below, built from the site name
and the digits-only relation id parsed out of the link — never to the URL
the user typed — and redirects are only followed within
waymarkedtrails.org.

The map's OSM route overlay asks the same API which routes run through a
clicked spot (`/api/v1/list/by_area`, bbox in EPSG:3857 metres; see
`list_in`) and for one route's details (`fetch_details`). Waymarked's
usage policy asks for moderate access and forbids mass downloads, so
these only ever run for one user action each, through the cache and
rate limits of `osm_lookup`.
"""

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from wishmap import gpx, remote_track
from wishmap.remote_track import RemoteTrackError, Track

Site = Literal["cycling", "mtb", "hiking", "riding", "skating", "slopes"]

SITE = "Waymarked Trails"
DOMAIN = "waymarkedtrails.org"
SITES: tuple[Site, ...] = ("cycling", "mtb", "hiking", "riding", "skating", "slopes")
# Every imported route gets this tag, so they can be toggled together.
TAG = "waymarked"
# OSM data is under the ODbL; its GPX carries this (the map shows the
# attribution while such routes are drawn, see index.html).
COPYRIGHT_AUTHOR = "OpenStreetMap contributors"
LICENSE = "https://opendatacommons.org/licenses/odbl/1-0/"

_HOSTS: dict[str, Site] = {f"{s}.{DOMAIN}": s for s in SITES}
_ID_RE = re.compile(r"^\d+$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_EARTH_RADIUS_M = 6378137.0
# Web mercator's latitude limit (the square world of the tiles).
_MAX_MERCATOR_LAT = 85.05112878
# Superroutes nest routes in routes; no real one is anywhere near this deep.
_MAX_DEPTH = 32
# A click lookup has a user waiting: give up sooner than an import does.
LOOKUP_TIMEOUT_S = 5.0
# Network levels, widest first (Waymarked's `group`).
GROUPS = ("INT", "NAT", "REG", "LOC")


def parse_url(url: str) -> tuple[Site, str] | None:
    """Return (site, digits-only relation id) for a Waymarked Trails route link.

        https://cycling.waymarkedtrails.org/#route?id=19216720&map=12/47.5/9.6
                                                    -> ("cycling", "19216720")
        https://hiking.waymarkedtrails.org/#route?type=relation&id=7
                                                    -> ("hiking", "7")
        https://hiking.waymarkedtrails.org/#route?id=7&type=way -> None
    """
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    site = _HOSTS.get(host)
    if site is None:
        return None
    page, _, query = parsed.fragment.partition("?")
    if page != "route":
        return None
    params = parse_qs(query)
    ids = params.get("id", [])
    if len(ids) != 1 or not _ID_RE.match(ids[0]):
        return None
    if params.get("type", ["relation"]) != ["relation"]:
        return None
    return site, ids[0]


def item_url(site: Site, relation_id: str) -> str:
    return f"https://{site}.{DOMAIN}/#route?id={relation_id}&type=relation"


def _download(url: str, timeout: float = remote_track.TIMEOUT_S) -> bytes:
    """GET `url` and return its body (bounded). Raises RemoteTrackError."""
    return remote_track.download(
        url,
        domain=DOMAIN,
        site=SITE,
        not_public="Waymarked Trails refused that route.",
        not_found="Waymarked Trails has no such route.",
        timeout=timeout,
    )


def to_mercator(lon: float, lat: float) -> tuple[float, float]:
    """[lon, lat] -> web mercator metres (the inverse of `_lon_lat`)."""
    lat = max(-_MAX_MERCATOR_LAT, min(_MAX_MERCATOR_LAT, lat))
    x = math.radians(lon) * _EARTH_RADIUS_M
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * _EARTH_RADIUS_M
    return x, y


def _lon_lat(x: object, y: object) -> list[float] | None:
    """Web mercator metres -> [lon, lat] (6 decimals), or None if malformed."""
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    lon = math.degrees(x / _EARTH_RADIUS_M)
    lat = math.degrees(2 * math.atan(math.exp(y / _EARTH_RADIUS_M)) - math.pi / 2)
    return remote_track.point(round(lon, 6), round(lat, 6))


def _way_points(way: Any) -> list[list[float]]:
    geometry = way.get("geometry") if isinstance(way, dict) else None
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(coords, list):
        return []
    points = (_lon_lat(*c) for c in coords if isinstance(c, list) and len(c) >= 2)
    return [p for p in points if p is not None]


def _collect(segment: Any, out: list[list[float]], depth: int = 0) -> None:
    """Append `segment`'s points to `out` in travel order.

    Ways are already oriented, so consecutive ones share an end point
    (dropped once). A split stretch follows its forward branch;
    appendices (side branches) are left out.
    """
    if not isinstance(segment, dict) or depth > _MAX_DEPTH:
        return
    kind = segment.get("route_type")
    children: Any = []
    if kind == "route":
        children = segment.get("main")
    elif kind == "split":
        children = segment.get("forward")
    elif kind == "linear":
        for way in segment.get("ways") or []:
            points = _way_points(way)
            if out and points and points[0] == out[-1]:
                points = points[1:]
            out.extend(points)
        return
    if isinstance(children, list):
        for child in children:
            _collect(child, out, depth + 1)


def _text(data: dict[str, Any], key: str, max_len: int = 500) -> str:
    """`data[key]` as stripped text ("" unless a string), capped."""
    value = data.get(key)
    return value.strip()[:max_len] if isinstance(value, str) else ""


def _description(data: dict[str, Any]) -> str:
    """A few lines from the relation: ref, operator, official website."""
    lines: list[str] = []
    for label, key in (("Ref", "ref"), ("Operator", "operator"), ("Website", "url")):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}: {value.strip()}")
    return "\n".join(lines)


def parse_track(site: Site, relation_id: str, data: Any) -> Track:
    """Build a Track from a relation's details JSON. Raises RemoteTrackError."""
    if not isinstance(data, dict):
        raise RemoteTrackError("Unexpected answer from Waymarked Trails.")
    points: list[list[float]] = []
    _collect(data.get("route"), points)
    if not points:
        raise RemoteTrackError("That Waymarked Trails route has no track.")
    name = data.get("name")
    ref = data.get("ref")
    if not (isinstance(name, str) and name.strip()):
        name = ref if isinstance(ref, str) else ""
    return Track(
        url=item_url(site, relation_id),
        name=name.strip(),
        description=_description(data),
        points=points,
    )


def _fetch_relation(site: Site, relation_id: str) -> Any:
    """Download a route relation's details JSON. Raises RemoteTrackError."""
    if site not in SITES:
        raise ValueError(f"unknown Waymarked Trails site {site!r}")
    if not _ID_RE.match(relation_id):
        raise ValueError("relation_id must be digits only")
    body = _download(f"https://{site}.{DOMAIN}/api/v1/details/relation/{relation_id}")
    try:
        return json.loads(body)
    except ValueError:
        raise RemoteTrackError("Unexpected answer from Waymarked Trails.")


def fetch_track(site: Site, relation_id: str) -> Track:
    """Download a route relation. Raises RemoteTrackError (or a subclass)."""
    return parse_track(site, relation_id, _fetch_relation(site, relation_id))


# ── The map overlay's click lookup ───────────────────────────────────


@dataclass(frozen=True)
class OsmRoute:
    """One route of a `list/by_area` answer."""

    site: Site
    id: str  # digits only
    name: str
    ref: str
    group: str  # one of GROUPS
    itinerary: tuple[str, ...]
    symbol_id: str | None  # safe for a URL path segment
    # Describes the waymark ("yellow diamonds"); all many local routes have.
    symbol_description: str

    @property
    def url(self) -> str:
        return item_url(self.site, self.id)


def parse_list(site: Site, data: Any) -> list[OsmRoute]:
    """The routes of a `list/by_area` answer; anything malformed is skipped."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    routes: list[OsmRoute] = []
    for item in results:
        if not isinstance(item, dict) or item.get("type") != "relation":
            continue
        raw_id = item.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
            continue
        rel_id = str(raw_id)
        if not _ID_RE.match(rel_id) or len(rel_id) > 20:
            continue
        group = item.get("group")
        symbol = item.get("symbol_id")
        stops = item.get("itinerary")
        itinerary = tuple(
            s.strip()[:100] for s in (stops if isinstance(stops, list) else [])
            if isinstance(s, str) and s.strip()
        )[:10]
        routes.append(
            OsmRoute(
                site=site,
                id=rel_id,
                name=_text(item, "name", 200),
                ref=_text(item, "ref", 50),
                group=group if isinstance(group, str) and group in GROUPS else "LOC",
                itinerary=itinerary,
                symbol_id=symbol if isinstance(symbol, str) and _SYMBOL_RE.match(symbol) else None,
                symbol_description=_text(item, "symbol_description", 200),
            )
        )
    return routes


Bbox = tuple[float, float, float, float]


def snap_bbox(west: float, south: float, east: float, north: float) -> Bbox:
    """The click box in mercator metres, snapped outward to a grid of its own size.

    At one zoom level a click box is the same size in mercator metres
    wherever it is, so neighbouring clicks snap to the same few grid
    boxes and share cache entries. The grid step is the box's larger
    side, rounded to two significant digits so float noise can't split
    the grid.
    """
    x1, y1 = to_mercator(west, south)
    x2, y2 = to_mercator(east, north)
    side = max(x2 - x1, y2 - y1, 1.0)
    step = float(f"{side:.2g}")
    return (
        math.floor(x1 / step) * step,
        math.floor(y1 / step) * step,
        math.ceil(x2 / step) * step,
        math.ceil(y2 / step) * step,
    )


def list_in(site: Site, bbox: Bbox, limit: int = 20) -> list[OsmRoute]:
    """The routes of `site` crossing `bbox` (mercator metres). Raises RemoteTrackError."""
    if site not in SITES:
        raise ValueError(f"unknown Waymarked Trails site {site!r}")
    box = ",".join(f"{v:.1f}" for v in bbox)
    body = _download(
        f"https://{site}.{DOMAIN}/api/v1/list/by_area?bbox={box}&limit={int(limit)}",
        timeout=LOOKUP_TIMEOUT_S,
    )
    try:
        data = json.loads(body)
    except ValueError:
        raise RemoteTrackError("Unexpected answer from Waymarked Trails.")
    return parse_list(site, data)


def list_at(
    site: Site, west: float, south: float, east: float, north: float, limit: int = 20
) -> list[OsmRoute]:
    """The routes of `site` crossing a lon/lat box. Raises RemoteTrackError."""
    x1, y1 = to_mercator(west, south)
    x2, y2 = to_mercator(east, north)
    return list_in(site, (x1, y1, x2, y2), limit)


@dataclass(frozen=True)
class OsmRouteDetails:
    """A route's track plus what the overlay popup shows about it."""

    track: Track
    name: str
    ref: str
    length_km: float
    operator: str
    website: str
    description: str


def parse_details(site: Site, relation_id: str, data: Any) -> OsmRouteDetails:
    """Build the details from a relation's JSON. Raises RemoteTrackError."""
    track = parse_track(site, relation_id, data)
    assert isinstance(data, dict)  # parse_track checked
    return OsmRouteDetails(
        track=track,
        name=track.name[:200],
        ref=_text(data, "ref", 50),
        length_km=round(gpx.length_m(track.coords) / 1000, 1),
        operator=_text(data, "operator", 200),
        website=_text(data, "url", 500),
        description=_text(data, "description", 2000),
    )


def fetch_details(site: Site, relation_id: str) -> OsmRouteDetails:
    """Download a route relation's details. Raises RemoteTrackError (or a subclass)."""
    return parse_details(site, relation_id, _fetch_relation(site, relation_id))
