"""Pick the importer for a pasted route link (`POST /api/routes/import`).

Each supported platform's module parses its own links; `resolve()` asks
them in turn and returns the first match, ready to fetch.
"""

from collections.abc import Callable
from dataclasses import dataclass

from wishmap import komoot, remote_track, ridewithgps, waymarked
from wishmap.remote_track import Track

# For the error message when no importer recognises a link.
SUPPORTED = (
    "a Ride with GPS trip or route (https://ridewithgps.com/trips/… or /routes/…), "
    "a Komoot tour (https://www.komoot.com/tour/… or /smarttour/…) "
    "or a Waymarked Trails route (https://cycling.waymarkedtrails.org/#route?id=…)"
)


@dataclass(frozen=True)
class Link:
    # Short platform key, part of the route id ("rwgps", "komoot", "waymarked").
    key: str
    site: str
    kind: str
    item_id: str
    fetch: Callable[[], Track]
    # Added to the tags the user gave (e.g. "waymarked", to toggle them together).
    tags: tuple[str, ...] = ()

    @property
    def route_id(self) -> str:
        """Stable per item, so importing the same link again replaces the route."""
        return f"web-{self.key}-{self.kind}-{self.item_id}"

    @property
    def fallback_name(self) -> str:
        return f"{self.site} {self.kind} {self.item_id}"

    def to_gpx(self, track: Track) -> str:
        return remote_track.to_gpx(track, creator=f"wishmap ({self.site} import)")


def resolve(url: str) -> Link | None:
    """The importer for `url`, or None if no platform recognises it."""
    if (rwgps := ridewithgps.parse_url(url)) is not None:
        rwgps_kind, rwgps_id = rwgps
        return Link(
            "rwgps",
            ridewithgps.SITE,
            rwgps_kind,
            rwgps_id,
            lambda: ridewithgps.fetch_track(rwgps_kind, rwgps_id),
        )
    if (km := komoot.parse_url(url)) is not None:
        km_kind, km_id, token = km
        return Link(
            "komoot",
            komoot.SITE,
            km_kind,
            km_id,
            lambda: komoot.fetch_track(km_kind, km_id, token),
        )
    if (wm := waymarked.parse_url(url)) is not None:
        wm_site, wm_id = wm
        return Link(
            "waymarked",
            waymarked.SITE,
            wm_site,
            wm_id,
            lambda: waymarked.fetch_track(wm_site, wm_id),
            tags=(waymarked.TAG,),
        )
    return None
