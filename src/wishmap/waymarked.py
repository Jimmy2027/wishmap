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
"""

import json
import math
import re
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from wishmap import remote_track
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
_EARTH_RADIUS_M = 6378137.0
# Superroutes nest routes in routes; no real one is anywhere near this deep.
_MAX_DEPTH = 32


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


def _download(url: str) -> bytes:
    """GET `url` and return its body (bounded). Raises RemoteTrackError."""
    return remote_track.download(
        url,
        domain=DOMAIN,
        site=SITE,
        not_public="Waymarked Trails refused that route.",
        not_found="Waymarked Trails has no such route.",
    )


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


def fetch_track(site: Site, relation_id: str) -> Track:
    """Download a route relation. Raises RemoteTrackError (or a subclass)."""
    if site not in SITES:
        raise ValueError(f"unknown Waymarked Trails site {site!r}")
    if not _ID_RE.match(relation_id):
        raise ValueError("relation_id must be digits only")
    body = _download(f"https://{site}.{DOMAIN}/api/v1/details/relation/{relation_id}")
    try:
        data = json.loads(body)
    except ValueError:
        raise RemoteTrackError("Unexpected answer from Waymarked Trails.")
    return parse_track(site, relation_id, data)
