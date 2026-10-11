"""Import a public Komoot tour or smart tour from its link.

Komoot has no open API (only one for approved partners), but the API its
own web app uses answers for public items without a login:
`https://api.komoot.de/v007/tours/<id>` (or `smart_tours/<id>`, for the
tours Komoot suggests) has the name, and `…/coordinates` the track
(`{"items": [{"lat", "lng", "alt", "t"}, ...]}`). That API is
undocumented, so everything here is defensive. Private tours answer 403;
a share link's `share_token` is passed along, which opens such a tour.

The requests always go to the fixed host below, built from the
digits-only id (and a token of safe characters) parsed out of the link —
never to the URL the user typed — and redirects are only followed within
komoot.de, so a pasted link can't make the server fetch anything else.
"""

import json
import re
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

from wishmap import remote_track
from wishmap.remote_track import RemoteTrackError, Track

Kind = Literal["tour", "smarttour"]

SITE = "Komoot"
DOMAIN = "komoot.de"
API_URL = "https://api.komoot.de/v007"
WEB_URL = "https://www.komoot.com"

_HOSTS = ("komoot.com", "www.komoot.com", "komoot.de", "www.komoot.de")
# An optional locale prefix (`/de-de/tour/1`), then the kind and the id.
_PATH_RE = re.compile(r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?/(tour|smarttour)/(\d+)(?:/|$)", re.I)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_API_KIND = {"tour": "tours", "smarttour": "smart_tours"}


def parse_url(url: str) -> tuple[Kind, str, str | None] | None:
    """Return (kind, digits-only id, share token or None) for a Komoot link.

        https://www.komoot.com/tour/123              -> ("tour", "123", None)
        https://www.komoot.de/de-de/tour/1?share_token=ab -> ("tour", "1", "ab")
        https://www.komoot.com/smarttour/9?ref=itd   -> ("smarttour", "9", None)
        https://example.com/tour/1                   -> None
    """
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if (parsed.hostname or "").lower() not in _HOSTS:
        return None
    match = _PATH_RE.match(parsed.path or "")
    if match is None:
        return None
    kind: Kind = "tour" if match.group(1).lower() == "tour" else "smarttour"
    tokens = parse_qs(parsed.query).get("share_token", [])
    token = tokens[0] if tokens and _TOKEN_RE.match(tokens[0]) else None
    return kind, match.group(2), token


def item_url(kind: Kind, item_id: str) -> str:
    return f"{WEB_URL}/{kind}/{item_id}"


def _download(url: str) -> bytes:
    """GET `url` and return its body (bounded). Raises RemoteTrackError."""
    return remote_track.download(
        url,
        domain=DOMAIN,
        site=SITE,
        not_public="That Komoot tour is not public — make it public, use its "
        "share link, or download its GPX and upload the file instead.",
        not_found="Komoot has no such tour.",
        accept="application/hal+json, application/json",
    )


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise RemoteTrackError("Unexpected answer from Komoot.")


def _points(raw: Any) -> list[list[float]]:
    """[lon, lat, ele?] from a coordinates answer, skipping malformed entries."""
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    points = (
        remote_track.point(p.get("lng"), p.get("lat"), p.get("alt"))
        for p in items
        if isinstance(p, dict)
    )
    return [p for p in points if p is not None]


def parse_track(kind: Kind, item_id: str, tour: Any, coordinates: Any) -> Track:
    """Build a Track from a tour and its coordinates. Raises RemoteTrackError."""
    if not isinstance(tour, dict):
        raise RemoteTrackError("Unexpected answer from Komoot.")
    points = _points(coordinates)
    if not points:
        raise RemoteTrackError("That Komoot tour has no track points.")
    name = tour.get("name")
    return Track(
        url=item_url(kind, item_id),
        name=name.strip() if isinstance(name, str) else "",
        description="",
        points=points,
    )


def fetch_track(kind: Kind, item_id: str, share_token: str | None = None) -> Track:
    """Download a public (or shared) tour. Raises RemoteTrackError (or a subclass)."""
    if not item_id.isdigit():
        raise ValueError("item_id must be digits only")
    if share_token is not None and not _TOKEN_RE.match(share_token):
        raise ValueError("share_token has unexpected characters")
    base = f"{API_URL}/{_API_KIND[kind]}/{item_id}"
    query = "?" + urlencode({"share_token": share_token}) if share_token else ""
    tour = _json(_download(base + query))
    coordinates = _json(_download(f"{base}/coordinates{query}"))
    return parse_track(kind, item_id, tour, coordinates)
