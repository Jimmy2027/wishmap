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
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO, Any, Literal
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

Kind = Literal["trip", "route"]

BASE_URL = "https://ridewithgps.com"

# A long trip is big: a 700 km, 8-day trip is ~20 MB of JSON.
MAX_JSON_BYTES = 64 * 1024 * 1024
TIMEOUT_S = 60

_PATH_RE = re.compile(r"^/(trips|routes)/(\d+)(?:[/.]|$)")


class RideWithGpsError(Exception):
    """A Ride with GPS item could not be imported (message is user-facing)."""


class NotPublicError(RideWithGpsError):
    pass


class NotFoundError(RideWithGpsError):
    pass


@dataclass(frozen=True)
class Track:
    kind: Kind
    id: str
    name: str
    description: str
    # [lon, lat, ele?] in recording order.
    points: list[list[float]]

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.kind}s/{self.id}"

    @property
    def coords(self) -> list[list[float]]:
        """[lon, lat] pairs, as `gpx.parse_gpx_data` returns them."""
        return [p[:2] for p in self.points]


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


class _SameSiteRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to ridewithgps.com over https."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urlparse(newurl)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            host == "ridewithgps.com" or host.endswith(".ridewithgps.com")
        ):
            raise RideWithGpsError("Ride with GPS redirected somewhere unexpected.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameSiteRedirects())


def _download(url: str) -> bytes:
    """GET `url` and return its body (bounded). Raises RideWithGpsError."""
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "wishmap"}
    )
    try:
        with _opener.open(req, timeout=TIMEOUT_S) as resp:
            body = resp.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise NotPublicError(
                "That Ride with GPS item is not public — make it public, "
                "or download its GPX and upload the file instead."
            )
        if e.code == 404:
            raise NotFoundError("Ride with GPS has no such trip or route.")
        raise RideWithGpsError(f"Ride with GPS answered HTTP {e.code}.")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RideWithGpsError("Could not reach Ride with GPS.")
    if len(body) > MAX_JSON_BYTES:
        raise RideWithGpsError("That Ride with GPS item is too large to import.")
    return body


def _points(raw: Any) -> list[list[float]]:
    """[lon, lat, ele?] from `track_points`, skipping malformed entries."""
    points: list[list[float]] = []
    if not isinstance(raw, list):
        return points
    for p in raw:
        if not isinstance(p, dict):
            continue
        x, y, e = p.get("x"), p.get("y"), p.get("e")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        if not (-180 <= x <= 180 and -90 <= y <= 90):
            continue
        point = [float(x), float(y)]
        if isinstance(e, (int, float)):
            point.append(float(e))
        points.append(point)
    return points


def parse_track(kind: Kind, item_id: str, data: Any) -> Track:
    """Build a Track from a trip/route JSON body. Raises RideWithGpsError."""
    if not isinstance(data, dict):
        raise RideWithGpsError("Unexpected answer from Ride with GPS.")
    # Older answers wrap the item: {"type": "trip", "trip": {...}}.
    inner = data.get(kind)
    if isinstance(inner, dict):
        data = inner
    points = _points(data.get("track_points"))
    if not points:
        raise RideWithGpsError("That Ride with GPS item has no track points.")
    name = data.get("name")
    description = data.get("description")
    return Track(
        kind=kind,
        id=item_id,
        name=name.strip() if isinstance(name, str) else "",
        description=description.strip() if isinstance(description, str) else "",
        points=points,
    )


def fetch_track(kind: Kind, item_id: str) -> Track:
    """Download a public trip/route. Raises RideWithGpsError (or a subclass)."""
    if not item_id.isdigit():
        raise ValueError("item_id must be digits only")
    body = _download(f"{BASE_URL}/{kind}s/{item_id}.json")
    try:
        data = json.loads(body)
    except ValueError:
        raise RideWithGpsError("Unexpected answer from Ride with GPS.")
    return parse_track(kind, item_id, data)


def to_gpx(track: Track) -> str:
    """Serialise a Track as a single-track GPX 1.1 document."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="wishmap (Ride with GPS import)" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <trk><name>{escape(track.name)}</name>",
        f"    <link href={quoteattr(track.url)}/>",
        "    <trkseg>",
    ]
    for p in track.points:
        if len(p) > 2:
            lines.append(f'      <trkpt lat="{p[1]}" lon="{p[0]}"><ele>{p[2]}</ele></trkpt>')
        else:
            lines.append(f'      <trkpt lat="{p[1]}" lon="{p[0]}"/>')
    lines += ["    </trkseg>", "  </trk>", "</gpx>", ""]
    return "\n".join(lines)
