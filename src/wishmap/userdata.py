"""Build a user's map data (pins + routes GeoJSON) and cache it on `UserState`.

`build()` is the pure, global-free pipeline shared by the server and the
static export: read the user's pins and routes from the DB, then build
both FeatureCollections. `ensure_loaded()` is the server's lazy cache in
front of it — the first read after startup (or after a sync invalidated the
state) reads every route's geometry from `track_cache` (well under a second),
parsing only the GPX files that are new or changed since they were cached —
a full parse costs ~50s CPU for ~1200 routes; later reads return the cached
snapshot.
"""

import logging
import time
from pathlib import Path

from wishmap import pins_store, routes_store, track_cache
from wishmap.geojson import pins_to_geojson, route_features_from_parts
from wishmap.gpx import parse_gpx, track_parts
from wishmap.models import Feature, FeatureCollection
from wishmap.state import AppContext, UserData, UserState

logger = logging.getLogger(__name__)


class UserDataError(Exception):
    """The user's map data could not be built (e.g. an unparsable GPX)."""


def build(
    base_path: Path, db_path: Path, user_id: int
) -> tuple[FeatureCollection, FeatureCollection]:
    """Build the pins and routes GeoJSON collections for `user_id`.

    The user's routes' `gpx` paths resolve against `base_path` (their file
    tree). Each route's display geometry comes from `track_cache` when its
    GPX is unchanged; otherwise the GPX is parsed — once, for both the
    route's line and its start pin — and the result cached. A route whose
    GPX is missing or broken (deleted or corrupted out-of-band) is skipped
    with a warning instead of failing the whole build.
    """
    started = time.perf_counter()
    pins_fc = pins_to_geojson(pins_store.get_all(db_path, user_id))
    cached = track_cache.get_all(db_path, user_id)
    fresh: dict[str, track_cache.Entry] = {}
    lines: list[Feature] = []
    routes = routes_store.get_all(db_path, user_id)
    for route in routes:
        path = base_path / route.gpx
        try:
            # Stat before parsing: should the file change in between, the
            # older stamp makes the next build parse it again.
            stamp = track_cache.Stamp.of(path)
            entry = cached.get(route.gpx)
            if entry is None or entry.stamp != stamp:
                entry = track_cache.Entry(stamp, track_parts(parse_gpx(path)))
                fresh[route.gpx] = entry
        except Exception as e:  # missing file, empty track, malformed XML
            logger.warning("Skipping route %r: GPX unusable (%s)", route.id, e)
            continue
        line, start = route_features_from_parts(route, entry.parts)
        lines.append(line)
        pins_fc.features.append(start)
    # Write only when something changed: new geometry, or stale entries.
    in_use = {r.gpx for r in routes}
    if fresh or not cached.keys() <= in_use:
        _save_cache(db_path, user_id, fresh, in_use)
    logger.info(
        "Loaded %d route(s) of user %d in %.2fs (%d GPX parsed, %d from cache)",
        len(lines),
        user_id,
        time.perf_counter() - started,
        len(fresh),
        len(lines) - len(fresh),
    )
    return pins_fc, FeatureCollection(features=lines)


def _save_cache(
    db_path: Path, user_id: int, fresh: dict[str, track_cache.Entry], keep: set[str]
) -> None:
    """Best effort: a failed cache write only makes the next build slower."""
    try:
        track_cache.update(db_path, user_id, fresh, keep)
    except Exception as e:
        logger.warning("Could not update the track cache of user %d: %s", user_id, e)


def _load(ctx: AppContext, user_id: int) -> UserData:
    """Build the user's data from the DB and their file tree.

    Wraps every failure in UserDataError so a bad GPX surfaces as a clean
    error on the request instead of killing the worker.
    """
    try:
        pins_fc, routes_fc = build(ctx.user_dir(user_id), ctx.db_path, user_id)
    except Exception as e:  # GPX parse (a file changed mid-build), DB errors
        raise UserDataError(f"Building the map failed: {e}") from e
    return UserData(pins_fc=pins_fc, routes_fc=routes_fc)


def ensure_loaded(ctx: AppContext, state: UserState) -> UserData:
    """Return the user's built data, building it first if the state is cold.

    Blocking (the build parses every GPX) — call from a threadpool handler.
    Concurrent callers on a cold state build once: the first takes
    `data_lock` and builds, the rest wait and reuse its result. On failure
    the state stays cold, so the next read retries.
    """
    data = state.data
    if data is not None:
        return data
    with state.data_lock:
        # Re-check under the lock: another request may have just built it.
        data = state.data
        if data is None:
            data = state.data = _load(ctx, state.user_id)
        return data
