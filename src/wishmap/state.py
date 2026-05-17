"""Per-user runtime state, held in a registry on `app.state`.

Everything the server keeps in memory between requests for one user lives
on a `UserState`: the built GeoJSON collections, the background sync's
status and lock, and a Garmin login parked on its MFA code. A
`StateRegistry` hands out one `UserState` per user id, created on first
use. The registry, the server-wide paths/settings and the login throttle
are bundled into an `AppContext` that `lifespan` stores as `app.state.ctx`, so a fresh
lifespan (a restart, or a new TestClient) starts from a clean slate — no
module globals to reset.

The GeoJSON is built lazily (see `userdata.ensure_loaded`): a state starts
*cold* (`data is None`), the first read builds it, `invalidate()` drops it
after a sync so the next read rebuilds, and the per-route swaps patch a
warm state in place (a no-op when cold — the next build reads the DB).

This module is a leaf: it imports nothing from wishmap but `models`,
`geojson` and `schema`, so `garmin`, `sync_runner` and `userdata` can all
depend on it.
"""

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wishmap.geojson import start_properties
from wishmap.models import Feature, FeatureCollection, FeatureProperties, ServerSettings
# Re-exported (`as` form): the owner of rows written before accounts existed.
from wishmap.schema import UNCLAIMED_USER_ID as UNCLAIMED_USER_ID

if TYPE_CHECKING:
    from garminconnect import Garmin


def initial_sync_status() -> dict[str, Any]:
    """Return a fresh sync status dict (shape documented in `sync_runner`)."""
    return {
        "state": "idle",
        "started_at": None,
        "finished_at": None,
        "services": {
            "garmin": {"state": "idle", "error": None},
        },
        "error": None,
        # True when Garmin raised MfaRequiredError this run; the frontend
        # then shows the MFA-code modal instead of an error tooltip.
        # Cleared by begin() on the next sync attempt.
        "needs_mfa": False,
        # True when Garmin needs the user's password (no saved session, or
        # it expired); the frontend then shows the Garmin login modal.
        # Cleared by begin() like needs_mfa.
        "needs_garmin_login": False,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PendingMfaSlot:
    """A Garmin credential login waiting for its MFA code, plus its account
    and start time.

    Filled by the sync that hit MFA and consumed by POST /api/garmin/mfa.
    One slot per user, so one user's half-finished login can never be
    resumed (or clobbered) by another's code. Thread-safe: the sync fills
    it from a worker thread while the MFA endpoint reads it from another.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entry: tuple["Garmin", str, float] | None = None

    def put(self, client: "Garmin", username: str) -> None:
        """Park `client`, logging into Garmin account `username` (replacing
        any older pending login)."""
        with self._lock:
            self._entry = (client, username, time.monotonic())

    def get(self, max_age_sec: float) -> "tuple[Garmin, str] | None":
        """Return the pending client and its Garmin account, or None if empty
        or older than `max_age_sec`.

        An expired login is dropped, so it can't be resumed later either.
        """
        with self._lock:
            if self._entry is not None and (
                time.monotonic() - self._entry[2] > max_age_sec
            ):
                self._entry = None
            return None if self._entry is None else self._entry[:2]

    def discard(self, client: "Garmin") -> None:
        """Clear the slot, but only if it still holds `client`.

        A sync that started a newer login meanwhile keeps its own entry.
        """
        with self._lock:
            if self._entry is not None and self._entry[0] is client:
                self._entry = None

    def clear(self) -> None:
        """Drop whatever login is parked (the user switched or forgot accounts)."""
        with self._lock:
            self._entry = None

    def is_set(self) -> bool:
        """True if a login is parked (expiry is only checked by `get`)."""
        with self._lock:
            return self._entry is not None


@dataclass(frozen=True)
class UserData:
    """One user's built map data, swapped in and out as a single reference.

    Readers grab `state.data` once and use that snapshot; writers build a
    new `UserData` and rebind (atomic under the GIL), so a concurrent GET
    always serializes a complete collection, never a half-mutated list.
    """

    pins_fc: FeatureCollection
    routes_fc: FeatureCollection


@dataclass(eq=False)
class UserState:
    """All in-memory state for one user (see the module docstring)."""

    user_id: int
    # None = cold: the next read builds it (userdata.ensure_loaded).
    data: UserData | None = None
    # Serializes every *writer* of `data` against the others: the lazy
    # build, invalidate(), and the per-request add/edit/delete swaps. The build
    # holds it across its DB read AND the rebind, so a route added during
    # a ~50s build is either seen by that build's DB read or appended to
    # its result afterwards — never committed to SQLite yet lost from the
    # live map. Readers stay lock-free (see UserData).
    data_lock: threading.Lock = field(default_factory=threading.Lock)
    # Owned by sync_runner (shape documented there).
    sync_status: dict[str, Any] = field(default_factory=initial_sync_status)
    # Held by a running sync and by a Strava archive import, which both
    # rewrite the user's Garmin routes. Between syncs, begin() flipping
    # `sync_status` is the primary gate; this lock is its backstop and
    # makes a sync started during an import wait for it.
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_mfa: PendingMfaSlot = field(default_factory=PendingMfaSlot)

    def invalidate(self) -> None:
        """Drop the built data so the next read rebuilds it.

        Called after a sync wrote new data. Blocks while a build is in
        flight (so that build's stale result can't land after the drop) —
        call it off the event loop.
        """
        with self.data_lock:
            self.data = None

    def swap_in_route(self, route_feature: Feature, start_feature: Feature) -> None:
        """Add one route's line + start pin to the built data.

        Any prior features for the same id are removed first, so re-adding
        a route updates it in place. No-op when cold: the route is already
        in the DB, and the next build reads it from there. Deliberately not
        a full rebuild — that re-parses every GPX (~50s).
        """
        route_id = route_feature.properties.id
        start_id = start_feature.properties.id
        with self.data_lock:
            data = self.data
            if data is None:
                return
            routes = [f for f in data.routes_fc.features if f.properties.id != route_id]
            routes.append(route_feature)
            pins = [f for f in data.pins_fc.features if f.properties.id != start_id]
            pins.append(start_feature)
            self.data = replace(
                data,
                pins_fc=FeatureCollection(features=pins),
                routes_fc=FeatureCollection(features=routes),
            )

    def swap_out_route(self, route_id: str) -> None:
        """Remove one route's line + start pin from the built data (no-op when cold)."""
        start_id = f"{route_id}-start"
        with self.data_lock:
            data = self.data
            if data is None:
                return
            self.data = replace(
                data,
                pins_fc=FeatureCollection(
                    features=[f for f in data.pins_fc.features if f.properties.id != start_id]
                ),
                routes_fc=FeatureCollection(
                    features=[f for f in data.routes_fc.features if f.properties.id != route_id]
                ),
            )

    def update_route_properties(
        self, load: Callable[[], FeatureProperties | None]
    ) -> None:
        """Give one route's line + start pin its current properties (an edit).

        `load` re-reads the route's properties from the DB (None once it's
        deleted) and runs under `data_lock`, like `refresh_pin` — so of two
        concurrent edits the later commit wins on the map too. The geometry
        stays: an edit never touches the GPX. No-op when cold, when the
        route is gone, or when it isn't on the map (its GPX is unusable).
        """

        def patched(f: Feature, new: FeatureProperties) -> Feature:
            return f.model_copy(update={"properties": new})

        with self.data_lock:
            data = self.data
            if data is None:
                return
            props = load()
            if props is None:
                return
            start_props = start_properties(props)
            self.data = replace(
                data,
                pins_fc=FeatureCollection(
                    features=[
                        patched(f, start_props)
                        if f.properties.kind == "route_start"
                        and f.properties.id == start_props.id
                        else f
                        for f in data.pins_fc.features
                    ]
                ),
                routes_fc=FeatureCollection(
                    features=[
                        patched(f, props) if f.properties.id == props.id else f
                        for f in data.routes_fc.features
                    ]
                ),
            )

    def refresh_pin(self, pin_id: str, load: Callable[[], Feature | None]) -> None:
        """Show one of the user's own pins as the DB has it now (an add or edit).

        `load` re-reads the pin's feature (None once it's deleted) and runs
        under `data_lock`, so the swap reflects the latest commit: swapping
        in the request's own copy instead could put back a pin a concurrent
        DELETE just removed, or let an older of two concurrent edits win.
        The pin replaces any feature with its id, else is appended; a
        deleted one is removed. No-op when cold.
        """
        with self.data_lock:
            data = self.data
            if data is None:
                return
            pins = [f for f in data.pins_fc.features if not _is_pin(f, pin_id)]
            feature = load()
            if feature is not None:
                pins.append(feature)
            self.data = replace(data, pins_fc=FeatureCollection(features=pins))

    def swap_out_pin(self, pin_id: str) -> None:
        """Remove one of the user's own pins (no-op when cold)."""
        with self.data_lock:
            data = self.data
            if data is None:
                return
            self.data = replace(
                data,
                pins_fc=FeatureCollection(
                    features=[f for f in data.pins_fc.features if not _is_pin(f, pin_id)]
                ),
            )


def _is_pin(feature: Feature, pin_id: str) -> bool:
    """True for the user's pin `pin_id` (never a derived route-start pin)."""
    return feature.properties.kind == "pin" and feature.properties.id == pin_id


class StateRegistry:
    """Hands out one `UserState` per user id, creating it on first use."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[int, UserState] = {}

    def get(self, user_id: int) -> UserState:
        with self._lock:
            state = self._states.get(user_id)
            if state is None:
                state = self._states[user_id] = UserState(user_id=user_id)
            return state


@dataclass
class _ThrottleEntry:
    failures: int = 0  # consecutive failures
    last: float = 0.0  # time of the last failure
    until: float = 0.0  # locked out until this time
    pending: int = 0  # attempts begun but not yet recorded


class LoginThrottle:
    """In-memory brute-force brake on password checks, keyed per username.

    After `max_failures` consecutive failures for a key, every attempt is
    refused for `lockout_sec` — even with the right password — then the
    key starts fresh. A success clears the key; failures further apart
    than `window_sec` don't add up. Thread-safe (login handlers run on the
    threadpool). In-memory on purpose: a restart forgets, which only ever
    errs towards letting a user back in.

    Callers reserve each attempt with `begin` *before* the slow password
    check and finish it with exactly one of `record_failure`,
    `record_success` or `cancel`. In-flight attempts count towards the
    limit, so a burst of parallel requests gets no more than
    `max_failures` guesses in — not one per worker thread.
    """

    # Above this many tracked keys, stale ones are pruned on the next failure.
    _PRUNE_ABOVE = 10_000
    # Retry-After for an attempt refused only because others are in flight
    # (they settle within one password check).
    _BUSY_RETRY_SEC = 1.0

    def __init__(
        self,
        max_failures: int = 5,
        lockout_sec: float = 60.0,
        window_sec: float = 15 * 60.0,
    ) -> None:
        self.max_failures = max_failures
        self.lockout_sec = lockout_sec
        self.window_sec = window_sec
        # Replaceable in tests.
        self.clock: Callable[[], float] = time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, _ThrottleEntry] = {}

    def retry_after(self, key: str) -> float:
        """Seconds until `key` may try again; 0 when it isn't locked out."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return 0.0
            return max(0.0, entry.until - self.clock())

    def begin(self, key: str) -> float:
        """Reserve one password check for `key`.

        Returns 0 when reserved (the caller must then record its outcome),
        else the seconds to wait: while locked out, or while enough
        attempts are in flight to reach the limit should they all fail.
        """
        now = self.clock()
        with self._lock:
            entry = self._entries.setdefault(key, _ThrottleEntry())
            if entry.until > now:
                return entry.until - now
            if now - entry.last > self.window_sec:
                entry.failures = 0
            if entry.failures + entry.pending >= self.max_failures:
                return self._BUSY_RETRY_SEC
            entry.pending += 1
            return 0.0

    def record_failure(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            entry = self._entries.setdefault(key, _ThrottleEntry(last=now))
            entry.pending = max(0, entry.pending - 1)
            if entry.until > now:
                # Already locked (by a parallel attempt): keep that lockout.
                return
            if now - entry.last > self.window_sec:
                entry.failures = 0
            entry.failures += 1
            entry.last = now
            if entry.failures >= self.max_failures:
                # Lock, and start counting afresh once the lockout ends.
                entry.failures = 0
                entry.until = now + self.lockout_sec
            if len(self._entries) > self._PRUNE_ABOVE:
                self._prune(now)

    def record_success(self, key: str) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            if entry.pending > 1:
                # Others are still in flight: keep their reservations.
                self._entries[key] = _ThrottleEntry(pending=entry.pending - 1)
            else:
                del self._entries[key]

    def cancel(self, key: str) -> None:
        """Release a reservation whose check never completed (it raised)."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.pending = max(0, entry.pending - 1)

    def _prune(self, now: float) -> None:
        stale = [
            k
            for k, e in self._entries.items()
            if e.pending == 0 and e.until <= now and now - e.last > self.window_sec
        ]
        for k in stale:
            del self._entries[k]


@dataclass(frozen=True)
class AppContext:
    """Server-wide state set up by `lifespan`, stored as `app.state.ctx`."""

    registry: StateRegistry
    settings: ServerSettings
    # The server's TOML and its directory.
    config_path: Path
    base_path: Path
    # App-managed state: `db_path` (SQLite) and the per-user file trees
    # (see `user_dir`) live under `data_root`.
    data_root: Path
    db_path: Path
    login_throttle: LoginThrottle = field(default_factory=LoginThrottle)

    def user_dir(self, user_id: int) -> Path:
        """The user's file tree; their routes' `gpx` paths are relative to it."""
        return user_dir(self.data_root, user_id)


def user_dir(data_root: Path, user_id: int) -> Path:
    """The file tree of `user_id` under `data_root` (`<data_root>/users/<id>`).

    For code without an `AppContext` (the CLI's import and export).
    """
    return data_root / "users" / str(user_id)
