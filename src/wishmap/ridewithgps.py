"""Import a public Ride with GPS trip or route from its link.

No API key or login: a public trip/route's page has a `.json` twin
(`https://ridewithgps.com/trips/<id>.json`) carrying its name,
description and `track_points` (`{"x": lon, "y": lat, "e": ele, ...}`).
That endpoint is undocumented, so everything here is defensive. Private
items answer 401/403 and are reported as such; the documented API
(OAuth) would be needed for them.

The request always goes to the fixed host below, built from the
digits-only id parsed out of the link — never to the URL the user typed —
and redirects are only followed within ridewithgps.com, so a pasted link
can't make the server fetch anything else.
"""

import json
import re
from typing import Any, Literal
from urllib.parse import urlparse

from wishmap import remote_track
from wishmap.remote_track import RemoteTrackError, Track

Kind = Literal["trip", "route"]

SITE = "Ride with GPS"
DOMAIN = "ridewithgps.com"
BASE_URL = "https://ridewithgps.com"

_PATH_RE = re.compile(r"^/(trips|routes)/(\d+)(?:[/.]|$)")


def parse_url(url: str) -> tuple[Kind, str] | None:
    """Return (kind, digits-only id) for a ridewithgps.com trip/route link.

        https://ridewithgps.com/trips/100124261        -> ("trip", "100124261")
        https://www.ridewithgps.com/routes/42?x=1      -> ("route", "42")
        https://example.com/trips/1                    -> None
    """
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host not in ("ridewithgps.com", "www.ridewithgps.com"):
        return None
    match = _PATH_RE.match(parsed.path or "")
    if match is None:
        return None
    kind: Kind = "trip" if match.group(1) == "trips" else "route"
    return kind, match.group(2)


def item_url(kind: Kind, item_id: str) -> str:
    return f"{BASE_URL}/{kind}s/{item_id}"


def _download(url: str) -> bytes:
    """GET `url` and return its body (bounded). Raises RemoteTrackError."""
    return remote_track.download(
        url,
        domain=DOMAIN,
        site=SITE,
        not_public="That Ride with GPS item is not public — make it public, "
        "or download its GPX and upload the file instead.",
        not_found="Ride with GPS has no such trip or route.",
    )


def _points(raw: Any) -> list[list[float]]:
    """[lon, lat, ele?] from `track_points`, skipping malformed entries."""
    if not isinstance(raw, list):
        return []
    points = (
        remote_track.point(p.get("x"), p.get("y"), p.get("e"))
        for p in raw
        if isinstance(p, dict)
    )
    return [p for p in points if p is not None]


def parse_track(kind: Kind, item_id: str, data: Any) -> Track:
    """Build a Track from a trip/route JSON body. Raises RemoteTrackError."""
    if not isinstance(data, dict):
        raise RemoteTrackError("Unexpected answer from Ride with GPS.")
    # Older answers wrap the item: {"type": "trip", "trip": {...}}.
    inner = data.get(kind)
    if isinstance(inner, dict):
        data = inner
    points = _points(data.get("track_points"))
    if not points:
        raise RemoteTrackError("That Ride with GPS item has no track points.")
    name = data.get("name")
    description = data.get("description")
    return Track(
        url=item_url(kind, item_id),
        name=name.strip() if isinstance(name, str) else "",
        description=description.strip() if isinstance(description, str) else "",
        points=points,
    )


def fetch_track(kind: Kind, item_id: str) -> Track:
    """Download a public trip/route. Raises RemoteTrackError (or a subclass)."""
    if not item_id.isdigit():
        raise ValueError("item_id must be digits only")
    body = _download(f"{item_url(kind, item_id)}.json")
    try:
        data = json.loads(body)
    except ValueError:
        raise RemoteTrackError("Unexpected answer from Ride with GPS.")
    return parse_track(kind, item_id, data)
