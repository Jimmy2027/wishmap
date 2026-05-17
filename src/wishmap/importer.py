"""Import a single-user wishmap install into an account (`wishmap import-toml`).

Before accounts, wishmap was configured by one `wishmap.toml` — plus the
`data/garmin/garmin.toml` a Garmin sync wrote, pulled in via `includes` —
and kept web-added routes and ratings in `data/wishmap.db`. `import_toml`
moves such an install into one account:

  - the TOML's pins, and its routes with their GPX files copied into the
    user's file tree. A Garmin sync's `garmin-<activity>` routes get
    source 'garmin' and land in `gpx/garmin/<file>`, where the account's
    own Garmin sync keeps its files (so it won't download them again);
    every other route gets source 'import' and lands in
    `gpx/import/<its path in the TOML>`;
  - if asked to claim them, the rows the legacy DB migration left to the
    unclaimed user (web routes, ratings — see `schema.py`), each route's
    GPX copied to `gpx/<source>/<file>`. Those rows belong to the server's
    own pre-account install, so the CLI claims them only when importing
    the server's config (or with an explicit `--claim`);
  - the Strava activities DB, which the Garmin sync matches against;
  - the [garmin] account name and settings — never a password or token.

Re-running is safe: routes and pins are upserted by id (so the TOML's
fields win over edits made since), GPX files that are already up to date
are not copied again, and the activities are merged. The source install
is only read — its files are copied, never moved or deleted.
"""

import filecmp
import hashlib
import logging
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from wishmap import db, integrations_store, pins_store, routes_store, strava, users
from wishmap.config import load_user_toml
from wishmap.models import GarminConfig, RouteConfig, WishmapConfig
from wishmap.routes_store import Source
from wishmap.schema import UNCLAIMED_USER_ID
from wishmap.state import user_dir

logger = logging.getLogger(__name__)

# Route ids a Garmin sync generated (`garmin.py` writes `garmin-<activityId>`).
GARMIN_ID_PREFIX = "garmin-"

# Where the single-user app kept its Strava activities DB, relative to the
# TOML, when the TOML has no [strava] section saying otherwise.
DEFAULT_STRAVA_DIR = "data/strava"

# The tables holding rows the unclaimed user can own.
_CLAIMABLE_TABLES = ("routes", "route_ratings", "pins")


class ImportTomlError(Exception):
    """The import cannot proceed (user-readable message)."""


@dataclass
class ImportResult:
    username: str
    user_created: bool
    # Pins / routes (per source) read from the TOML and written.
    pins: int = 0
    routes: dict[Source, int] = field(default_factory=dict)
    # How many of those routes did not exist in the account before.
    new_routes: int = 0
    # GPX files actually copied (those already up to date are skipped).
    gpx_copied: int = 0
    # Rows taken over from the unclaimed user.
    claimed_routes: int = 0
    claimed_ratings: int = 0
    claimed_pins: int = 0
    # Unclaimed rows left behind because the account already has that id.
    unclaimed_conflicts: int = 0
    # Activities merged into the account's Strava DB (None: no DB found).
    strava_activities: int | None = None
    # The Garmin account name written to the integration, if any.
    garmin_username: str | None = None


@dataclass(frozen=True)
class _Copy:
    src: Path
    # Relative to the user's dir (the route's new `gpx`).
    dest: PurePosixPath


def _toml_route_dest(
    route: RouteConfig, toml_base: Path
) -> tuple[Source, PurePosixPath]:
    """Where a TOML route's GPX goes in the user's tree, and the route's source."""
    rel = PurePosixPath(os.path.normpath(route.gpx))
    if route.id.startswith(GARMIN_ID_PREFIX):
        return "garmin", PurePosixPath("gpx", "garmin", rel.name)
    if rel.is_absolute() or rel.parts[0] == "..":
        # Outside the TOML's directory: no relative path to keep. A digest
        # of the absolute path keeps same-named files apart, stably.
        digest = hashlib.sha256(
            str((toml_base / route.gpx).resolve()).encode()
        ).hexdigest()[:12]
        return "import", PurePosixPath("gpx", "import", "_external", digest, rel.name)
    return "import", PurePosixPath("gpx", "import", rel)


def _check_collisions(copies: list[_Copy]) -> None:
    """Refuse two different source files that would land on the same path."""
    seen: dict[PurePosixPath, Path] = {}
    for copy in copies:
        src = copy.src.resolve()
        other = seen.setdefault(copy.dest, src)
        if other != src:
            raise ImportTomlError(
                f"GPX files {other} and {src} would both be imported as {copy.dest}"
            )


def _copy_if_changed(src: Path, dest: Path) -> bool:
    """Copy `src` to `dest` unless it already holds the same file; True if copied.

    copy2 keeps the mtime, so a re-import's shallow compare (size + mtime)
    skips unchanged files without reading them. The copy goes to a temp
    name first, so an interrupted import never leaves a truncated GPX.
    """
    if dest.is_file() and filecmp.cmp(src, dest, shallow=True):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.importing")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)
    return True


def _create_account(
    db_path: Path, username: str, get_password: Callable[[], str]
) -> users.User:
    # Check the name before asking for a password.
    if not users.USERNAME_RE.fullmatch(username):
        raise ImportTomlError(users.USERNAME_RULES)
    try:
        return users.create_user(db_path, username, get_password())
    except ValueError as e:
        raise ImportTomlError(str(e)) from e
    except users.UsernameTakenError as e:
        raise ImportTomlError(f"Username {username!r} was just taken") from e


def _unclaimed_routes(
    db_path: Path, owned: set[str], legacy_base: Path
) -> tuple[dict[str, str], list[_Copy]]:
    """Plan the claim of the unclaimed user's routes.

    Returns {route id: new gpx} and the GPX copies to make. Legacy GPX
    paths are relative to the server's config directory (`legacy_base`).
    Ids in `owned` (the account's routes) are left out: those rows stay
    unclaimed.
    """
    new_gpx: dict[str, str] = {}
    copies: list[_Copy] = []
    sources: tuple[Source, ...] = ("web", "garmin", "import")
    for source in sources:
        for route in routes_store.get_all(db_path, UNCLAIMED_USER_ID, source):
            if route.id in owned:
                continue
            dest = PurePosixPath("gpx", source, PurePosixPath(route.gpx).name)
            new_gpx[route.id] = dest.as_posix()
            src = legacy_base / route.gpx
            if src.is_file():
                copies.append(_Copy(src, dest))
            else:
                # Claimed anyway (keeps the row and its rating); it stays
                # off the map, as it was before, until its GPX is restored.
                logger.warning("Route %r: GPX %s not found, not copied", route.id, src)
    return new_gpx, copies


def _count_unclaimed(conn: sqlite3.Connection) -> int:
    return sum(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ?",
            (UNCLAIMED_USER_ID,),
        ).fetchone()[0]
        for table in _CLAIMABLE_TABLES
    )


def count_unclaimed(db_path: Path) -> int:
    """How many rows (routes, ratings, pins) the unclaimed user still has."""
    if not db_path.is_file():
        return 0
    with db.connect(db_path) as conn:
        return _count_unclaimed(conn)


def _claim(
    db_path: Path, user_id: int, new_gpx: dict[str, str]
) -> tuple[int, int, int, int]:
    """Give the unclaimed user's rows to `user_id`, in one transaction.

    Routes are moved only if listed in `new_gpx` (which also gives their
    new path); ratings and pins all move. `OR IGNORE` leaves a row behind
    when the account already has its id — the account's own row wins.
    Returns (routes, ratings, pins moved, rows left unclaimed).
    """
    with db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        routes = 0
        for route_id, gpx in new_gpx.items():
            routes += conn.execute(
                "UPDATE OR IGNORE routes SET user_id = ?, gpx = ? "
                "WHERE user_id = ? AND id = ?",
                (user_id, gpx, UNCLAIMED_USER_ID, route_id),
            ).rowcount
        ratings = conn.execute(
            "UPDATE OR IGNORE route_ratings SET user_id = ? WHERE user_id = ?",
            (user_id, UNCLAIMED_USER_ID),
        ).rowcount
        pins = conn.execute(
            "UPDATE OR IGNORE pins SET user_id = ? WHERE user_id = ?",
            (user_id, UNCLAIMED_USER_ID),
        ).rowcount
        left = _count_unclaimed(conn)
    return routes, ratings, pins, left


def _import_strava(
    config: WishmapConfig, toml_base: Path, udir: Path
) -> int | None:
    strava_dir = DEFAULT_STRAVA_DIR if config.strava is None else config.strava.gpx_dir
    src = toml_base / strava_dir / "activities.db"
    if not src.is_file():
        return None
    return strava.copy_activities(src, udir / "strava" / "activities.db")


def _import_garmin_settings(db_path: Path, user_id: int, garmin: GarminConfig) -> None:
    """Write the [garmin] account name and settings (never the password).

    Skipped when already stored as-is, so a re-import doesn't touch the row.
    """
    settings = {
        "activity_limit": garmin.activity_limit,
        "sport_filter": garmin.sport_filter,
    }
    current = integrations_store.get(db_path, user_id, "garmin")
    if (
        current is not None
        and current.username == garmin.username
        and current.settings == settings
    ):
        return
    integrations_store.upsert_settings(
        db_path, user_id, "garmin", username=garmin.username, settings=settings
    )


def import_toml(
    toml_path: Path,
    username: str,
    *,
    db_path: Path,
    data_root: Path,
    legacy_base: Path,
    get_password: Callable[[], str],
    claim: bool,
) -> ImportResult:
    """Import the install configured by `toml_path` into account `username`.

    See the module docstring for what is imported. The account is created
    if missing, with the password `get_password()` returns (only called
    then). `claim` gives the account the unclaimed DB rows (whose GPX
    paths are relative to `legacy_base`, the server's config directory);
    `claim=False` leaves those rows alone.

    The TOML is validated (and every GPX file checked) before anything is
    written. Raises ConfigError for a bad TOML and ImportTomlError for
    anything else that stops the import.
    """
    config = load_user_toml(toml_path)
    toml_base = toml_path.parent

    routes_by_source: dict[Source, list[RouteConfig]] = {}
    copies: list[_Copy] = []
    for route in config.routes:
        source, dest = _toml_route_dest(route, toml_base)
        copies.append(_Copy(toml_base / route.gpx, dest))
        routes_by_source.setdefault(source, []).append(
            route.model_copy(update={"gpx": dest.as_posix()})
        )
    _check_collisions(copies)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    user = users.get_by_username(db_path, username)
    new_gpx: dict[str, str] = {}
    if claim:
        # The TOML's routes are written first and win over unclaimed ones.
        owned = {r.id for r in config.routes}
        if user is not None:
            owned |= {r.id for r in routes_store.get_all(db_path, user.id)}
        new_gpx, claim_copies = _unclaimed_routes(db_path, owned, legacy_base)
        copies += claim_copies
    _check_collisions(copies)

    # Everything checked: from here on, write.
    created = user is None
    if user is None:
        user = _create_account(db_path, username, get_password)
    udir = user_dir(data_root, user.id)
    result = ImportResult(username=user.username, user_created=created)

    # Files first, then the rows that point at them: an interrupted import
    # leaves at most unreferenced files, which a re-run reuses.
    for copy in copies:
        if _copy_if_changed(copy.src, udir / copy.dest):
            result.gpx_copied += 1

    for source, routes in routes_by_source.items():
        result.new_routes += routes_store.upsert_many(db_path, user.id, source, routes)
        result.routes[source] = len(routes)
    for pin in config.pins:
        pins_store.insert(db_path, user.id, pin)
    result.pins = len(config.pins)

    if claim:
        (
            result.claimed_routes,
            result.claimed_ratings,
            result.claimed_pins,
            result.unclaimed_conflicts,
        ) = _claim(db_path, user.id, new_gpx)

    result.strava_activities = _import_strava(config, toml_base, udir)

    if config.garmin is not None:
        _import_garmin_settings(db_path, user.id, config.garmin)
        result.garmin_username = config.garmin.username

    logger.info(
        "Imported %s into account %r: %d pin(s), %d route(s), %d GPX file(s) copied",
        toml_path,
        user.username,
        result.pins,
        sum(result.routes.values()),
        result.gpx_copied,
    )
    return result
