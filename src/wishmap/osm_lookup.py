"""The server side of the map's OSM route overlay lookups (Waymarked Trails).

The browser loads Waymarked's overlay tiles itself, but its JSON API
sends no CORS header, so the two lookups a click needs go through us:
which routes run through the clicked spot (`routes_at`) and one route's
details and geometry (`details`). Waymarked's usage policy asks for
moderate access, so `OsmLookup` (one per server, on `AppContext`) keeps
every upstream call to a user action and as few as possible:

- TTL+LRU caches: lists for an hour, details for a day. The click box is
  snapped to a grid (`waymarked.snap_bbox`) so neighbouring clicks share
  entries.
- Coalescing: concurrent requests for one key share one upstream call.
- A server-wide token bucket on upstream calls, and a per-user limit on
  lookups (cached ones included): over either, our API answers 429.
- A circuit breaker: when Waymarked answers 429/503, *every* upstream
  call pauses for its Retry-After; meanwhile misses answer 503 (cached
  hits are still served).

All in memory and thread-safe (the endpoints run on the threadpool); a
restart forgets, which is fine.
"""

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from wishmap import gpx, waymarked
from wishmap.models import MultiLineStringGeometry, OsmRouteDetailsResponse
from wishmap.remote_track import RateLimitedError
from wishmap.waymarked import Bbox, OsmRoute, OsmRouteDetails, Site

V = TypeVar("V")


class LookupRefused(Exception):
    """We won't ask upstream now: answer `status` with `detail` (user-facing)."""

    def __init__(self, status: int, detail: str, retry_after: float) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


class _TtlLru(Generic[V]):
    """A bounded cache whose entries expire `ttl` seconds after they were put."""

    def __init__(self, ttl: float, max_items: int, clock: Callable[[], float]) -> None:
        self.ttl = ttl
        self.max_items = max_items
        self._clock = clock
        self._lock = threading.Lock()
        self._items: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()

    def get(self, key: Hashable) -> V | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if entry[0] <= self._clock():
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return entry[1]

    def put(self, key: Hashable, value: V) -> None:
        with self._lock:
            self._items[key] = (self._clock() + self.ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)


@dataclass
class _Flight:
    """One upstream call in progress, which identical requests wait for."""

    done: threading.Event = field(default_factory=threading.Event)
    value: object = None
    error: BaseException | None = None


def details_response(site: Site, relation_id: str, d: OsmRouteDetails) -> OsmRouteDetailsResponse:
    """What GET /api/osm-routes/{site}/{id} answers (geometry thinned like own routes)."""
    return OsmRouteDetailsResponse(
        site=site,
        id=relation_id,
        name=d.name,
        ref=d.ref,
        length_km=d.length_km,
        operator=d.operator,
        website=d.website,
        description=d.description,
        url=d.track.url,
        geometry=MultiLineStringGeometry(coordinates=gpx.track_parts(d.track.coords)),
    )


class OsmLookup:
    """Cached, coalesced, rate-limited access to Waymarked's JSON API (see module)."""

    # Wait at most this long for an upstream token before refusing (429):
    # one click with every site ticked asks six times in a row.
    _MAX_TOKEN_WAIT_S = 2.0

    def __init__(
        self,
        *,
        rate_per_s: float = 2.0,
        burst: int = 5,
        user_limit: int = 30,
        user_window_s: float = 60.0,
        list_ttl_s: float = 3600.0,
        list_max: int = 2000,
        details_ttl_s: float = 24 * 3600.0,
        details_max: int = 200,
    ) -> None:
        # Replaceable in tests.
        self.clock: Callable[[], float] = time.monotonic
        self.sleep: Callable[[float], None] = time.sleep
        self.rate_per_s = rate_per_s
        self.burst = burst
        self.user_limit = user_limit
        self.user_window_s = user_window_s
        now = lambda: self.clock()  # noqa: E731 (follows a replaced clock)
        self._lists: _TtlLru[list[OsmRoute]] = _TtlLru(list_ttl_s, list_max, now)
        self._details: _TtlLru[OsmRouteDetailsResponse] = _TtlLru(details_ttl_s, details_max, now)
        self._lock = threading.Lock()
        self._flights: dict[Hashable, _Flight] = {}
        self._tokens = float(burst)
        self._tokens_at: float | None = None
        self._paused_until = 0.0
        self._user_hits: dict[int, deque[float]] = {}

    # ── Limits ──

    def admit(self, user_id: int) -> None:
        """Count one lookup for `user_id`; LookupRefused (429) over the limit."""
        now = self.clock()
        with self._lock:
            hits = self._user_hits.setdefault(user_id, deque())
            while hits and hits[0] <= now - self.user_window_s:
                hits.popleft()
            if len(hits) >= self.user_limit:
                wait = hits[0] + self.user_window_s - now
                raise LookupRefused(
                    429, "Too many route lookups; wait a moment.", max(1.0, wait)
                )
            hits.append(now)
            if len(self._user_hits) > 10_000:
                self._user_hits = {
                    uid: h for uid, h in self._user_hits.items()
                    if h and h[-1] > now - self.user_window_s
                }

    def _paused(self, now: float) -> LookupRefused | None:
        if self._paused_until > now:
            wait = self._paused_until - now
            return LookupRefused(
                503,
                f"Waymarked Trails asked us to slow down; try again in {math.ceil(wait)} s.",
                wait,
            )
        return None

    def _take_token(self) -> None:
        """Reserve one upstream call: wait briefly for a token, or refuse."""
        with self._lock:
            now = self.clock()
            if (refused := self._paused(now)) is not None:
                raise refused
            if self._tokens_at is not None:
                self._tokens = min(
                    float(self.burst), self._tokens + (now - self._tokens_at) * self.rate_per_s
                )
            self._tokens_at = now
            wait = (1.0 - self._tokens) / self.rate_per_s if self._tokens < 1.0 else 0.0
            if wait > self._MAX_TOKEN_WAIT_S:
                raise LookupRefused(429, "The route lookup is busy; try again shortly.", wait)
            self._tokens -= 1.0
        if wait > 0:
            self.sleep(wait)

    def _pause(self, seconds: float) -> None:
        with self._lock:
            self._paused_until = max(self._paused_until, self.clock() + seconds)

    # ── Cached, coalesced upstream calls ──

    def _get(self, cache: _TtlLru[V], key: Hashable, fetch: Callable[[], V]) -> V:
        if (hit := cache.get(key)) is not None:
            return hit
        with self._lock:
            flight = self._flights.get(key)
            leader = flight is None
            if flight is None:
                flight = self._flights[key] = _Flight()
        if not leader:
            # Bounded: the leader's own download times out well before this.
            if not flight.done.wait(timeout=120):
                raise LookupRefused(503, "The route lookup timed out.", 5.0)
            if flight.error is not None:
                raise flight.error
            return flight.value  # type: ignore[return-value]
        try:
            value = cache.get(key)  # a flight that landed since our miss
            if value is None:
                self._take_token()
                try:
                    value = fetch()
                except RateLimitedError as e:
                    self._pause(e.retry_after)
                    raise
                cache.put(key, value)
            flight.value = value
            return value
        except BaseException as e:
            flight.error = e
            raise
        finally:
            with self._lock:
                del self._flights[key]
            flight.done.set()

    def routes_at(self, site: Site, bbox: Bbox) -> list[OsmRoute]:
        """The routes of `site` in `bbox` (a `waymarked.snap_bbox` result).

        Raises LookupRefused, or RemoteTrackError from upstream.
        """
        return self._get(self._lists, ("list", site, bbox), lambda: waymarked.list_in(site, bbox))

    def details(self, site: Site, relation_id: str) -> OsmRouteDetailsResponse:
        """One route's details and display geometry.

        Raises LookupRefused, or RemoteTrackError from upstream.
        """
        return self._get(
            self._details,
            ("details", site, relation_id),
            lambda: details_response(
                site, relation_id, waymarked.fetch_details(site, relation_id)
            ),
        )
