"""What the link importers (`ridewithgps`, `komoot`) share.

A `Track` (name, description, [lon, lat, ele?] points), the errors an
import can end in (messages are user-facing), a bounded JSON download
that only follows redirects within the platform's own domain, and the
GPX serialisation of a track.
"""

import importlib.metadata
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

# A long trip is big: a 700 km, 8-day Ride with GPS trip is ~20 MB of JSON.
MAX_JSON_BYTES = 64 * 1024 * 1024
TIMEOUT_S = 60
# Upstream's Retry-After on a 429/503: used when it sends none (or a date),
# and capped so a bogus value can't silence a platform for days.
DEFAULT_RETRY_AFTER_S = 60.0
MAX_RETRY_AFTER_S = 15 * 60.0


def _version() -> str:
    try:
        return importlib.metadata.version("wishmap")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


# Sent with every outgoing request, so the platforms can tell who is asking
# (Waymarked Trails' usage policy asks for moderate, identifiable access).
USER_AGENT = f"wishmap/{_version()} (+https://github.com/Jimmy2027/wishmap)"


class RemoteTrackError(Exception):
    """A linked track could not be imported (message is user-facing)."""


class NotPublicError(RemoteTrackError):
    pass


class NotFoundError(RemoteTrackError):
    pass


class RateLimitedError(RemoteTrackError):
    """The platform answered 429 or 503: back off for `retry_after` seconds."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(value: str | None) -> float:
    """Parse a Retry-After header (delta-seconds), defaulted and capped."""
    try:
        seconds = float(value) if value is not None else DEFAULT_RETRY_AFTER_S
    except ValueError:
        # An HTTP-date, or junk: not worth parsing for a back-off.
        seconds = DEFAULT_RETRY_AFTER_S
    if seconds != seconds or seconds < 0:  # NaN, negative
        seconds = DEFAULT_RETRY_AFTER_S
    return min(seconds, MAX_RETRY_AFTER_S)


@dataclass(frozen=True)
class Track:
    # Canonical link to the item on its platform (goes into the notes).
    url: str
    name: str
    description: str
    # [lon, lat, ele?] in recording order.
    points: list[list[float]]

    @property
    def coords(self) -> list[list[float]]:
        """[lon, lat] pairs, as `gpx.parse_gpx_data` returns them."""
        return [p[:2] for p in self.points]


def same_site(url: str, domain: str) -> bool:
    """True for an https URL on `domain` or one of its subdomains."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == domain or host.endswith("." + domain))


class SameSiteRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to `domain` (or a subdomain) over https."""

    def __init__(self, domain: str, site: str) -> None:
        super().__init__()
        self.domain = domain
        self.site = site

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not same_site(newurl, self.domain):
            raise RemoteTrackError(f"{self.site} redirected somewhere unexpected.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(
    url: str,
    *,
    domain: str,
    site: str,
    not_public: str,
    not_found: str,
    accept: str = "application/json",
    timeout: float = TIMEOUT_S,
) -> bytes:
    """GET `url` (on `domain`) and return its body, bounded.

    `site` names the platform in messages; `not_public`/`not_found` are
    the messages for 401/403 and 404. Raises RemoteTrackError (or a
    subclass: RateLimitedError for 429/503).
    """
    if not same_site(url, domain):
        raise ValueError(f"{url!r} is not on {domain}")
    opener = urllib.request.build_opener(SameSiteRedirects(domain, site))
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise NotPublicError(not_public)
        if e.code == 404:
            raise NotFoundError(not_found)
        if e.code in (429, 503):
            raise RateLimitedError(
                f"{site} is busy; try again later.",
                retry_after_seconds(e.headers.get("Retry-After") if e.headers else None),
            )
        raise RemoteTrackError(f"{site} answered HTTP {e.code}.")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RemoteTrackError(f"Could not reach {site}.")
    if len(body) > MAX_JSON_BYTES:
        raise RemoteTrackError(f"{site}'s answer is too large.")
    return body


def point(lon: object, lat: object, ele: object = None) -> list[float] | None:
    """[lon, lat, ele?] if lon/lat are numbers in range, else None."""
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        return None
    if isinstance(lon, bool) or isinstance(lat, bool):
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    p = [float(lon), float(lat)]
    if isinstance(ele, (int, float)) and not isinstance(ele, bool):
        p.append(float(ele))
    return p


def to_gpx(track: Track, *, creator: str, copyright: tuple[str, str] | None = None) -> str:
    """Serialise a Track as a single-track GPX 1.1 document.

    `copyright` is (author, licence URL), written to the GPX metadata so
    the licence travels with the file (e.g. OSM data under the ODbL).
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<gpx version=\"1.1\" creator={quoteattr(creator)} "
        'xmlns="http://www.topografix.com/GPX/1/1">',
    ]
    if copyright is not None:
        author, license_url = copyright
        lines += [
            f"  <metadata><copyright author={quoteattr(author)}>"
            f"<license>{escape(license_url)}</license></copyright></metadata>",
        ]
    lines += [
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
