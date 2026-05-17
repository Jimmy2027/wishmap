"""Sync one user's activities from Garmin Connect into their routes.

A sync (`sync`) runs on a `GarminSyncContext` the caller builds for one
user: their Garmin account name and settings, the Garmin session tokens
saved by an earlier sync (already unsealed from the vault), and — only
when the user just typed it — their Garmin password. Nothing here touches
the vault or stores a secret: new tokens go out through `on_tokens`, and
the caller seals them.

Login order: the saved tokens first (no network login, no MFA); then, if
the user gave a password, a fresh credential login; otherwise
`GarminLoginRequiredError`, which makes the web UI ask for the password.
A credential login that needs an MFA code parks itself on the user's
`PendingMfaSlot` (`MfaRequiredError`); `complete_mfa` finishes it.

Garmin session tokens are a JSON string (`Client.dumps()`), and
`Garmin.login(tokenstore)` takes such a string directly when it is longer
than 512 characters (shorter means "a directory path"), so tokens are
padded past that before every login — no token files on disk.

Each activity becomes a route `garmin-<activityId>` with its GPX at
`gpx/garmin/<activityId>.gpx` in the user's dir; the whole set replaces the
user's previous Garmin routes (`routes_store.replace_source`). The fetched
activity summaries are kept next to the GPX files (`activities.json`), so
a later Strava archive upload can re-match them without asking Garmin.
"""

import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from wishmap import routes_store, strava
from wishmap.exceptions import GarminLoginRequiredError, MfaRequiredError, SyncError
from wishmap.models import RouteConfig, Status
from wishmap.state import PendingMfaSlot

logger = logging.getLogger("wishmap.garmin")

# Garmin activityType.typeKey -> wishmap_sport. The map's color is no longer
# stored per route: the frontend derives the line/marker color from the sport
# (see SPORT_COLORS in static/index.html), so this only normalizes type keys.
SPORT_MAP: dict[str, str] = {
    "hiking": "hiking",
    "running": "running",
    "trail_running": "trail_running",
    "cycling": "biking",
    "mountain_biking": "mountain_biking",
    "gravel_cycling": "gravel",
    "road_biking": "biking",
    "walking": "walking",
    "mountaineering": "mountaineering",
    "backcountry_skiing_snowboarding": "ski_touring",
    "resort_skiing_snowboarding": "skiing",
    "cross_country_skiing": "cross_country_skiing",
    "snowshoeing": "snowshoeing",
    "swimming": "swimming",
    "open_water_swimming": "swimming",
    "stand_up_paddleboarding": "sup",
    "rock_climbing": "climbing",
    "kayaking": "kayaking",
}


def _map_sport(type_key: str) -> str:
    """Return the wishmap_sport for a Garmin activity type key.

    Unknown keys pass through unchanged so a new Garmin activity type still
    renders (the frontend hashes unknown sport names into a stable color).
    """
    return SPORT_MAP.get(type_key, type_key)


# Where a user's Garmin GPX files live, relative to their dir. The route
# ids are `garmin-<activityId>`: the frontend parses that prefix to link
# to Garmin Connect (static/index.html popupHtml), so keep it stable.
GPX_SUBDIR = PurePosixPath("gpx", "garmin")
ROUTE_ID_PREFIX = "garmin-"
# The last sync's activity summaries (see `rematch_strava`).
ACTIVITY_INDEX = GPX_SUBDIR / "activities.json"
# Summary fields kept in the index: what `activities_to_routes` reads.
_INDEX_FIELDS = (
    "activityId",
    "activityName",
    "activityType",
    "startTimeLocal",
    "startTimeGMT",
    "distance",
    "duration",
)

# garminconnect's MAX_ACTIVITY_LIMIT for a single get_activities() call.
ACTIVITY_PAGE_SIZE = 1000

# Garmin's MFA codes expire; don't resume a login older than this.
MFA_PENDING_TTL_SEC = 15 * 60

# `Garmin.login(s)` reads `s` as token data only when len(s) > 512.
_TOKEN_STRING_MIN_LEN = 513


def _no_tokens_sink(tokens: str) -> None:
    pass


@dataclass(eq=False)
class GarminSyncContext:
    """Everything one user's Garmin sync needs (see the module docstring)."""

    user_id: int
    db_path: Path
    # The user's file tree; route GPX paths are relative to it.
    user_dir: Path
    # The Garmin account (email) to log into.
    username: str
    activity_limit: int = 1000
    # Garmin activity type keys to keep; empty = all.
    sport_filter: list[str] = field(default_factory=list)
    # Session tokens saved by an earlier login (plaintext JSON), if any.
    tokens_json: str | None = field(default=None, repr=False)
    # The Garmin password, only when the user just entered it. Used once.
    password: str | None = field(default=None, repr=False)
    # The user's Strava activities DB, to take names/sports from on a match.
    # Checked for existence only when matching: an archive upload may
    # create it while the sync waits for the user's sync lock.
    strava_db: Path | None = None
    # Where a credential login waiting for its MFA code is parked.
    pending: PendingMfaSlot = field(default_factory=PendingMfaSlot)
    # Receives the session tokens after every successful login (and again
    # if Garmin refreshed them during the sync), to be sealed and stored.
    on_tokens: Callable[[str], None] = _no_tokens_sink


def _pad_tokens(tokens_json: str) -> str:
    """Pad a token string so `Garmin.login` takes it as data, not a path.

    Trailing whitespace is valid JSON; real token strings (two JWTs) are
    far longer anyway, this only guards the library's length heuristic.
    """
    return tokens_json.ljust(_TOKEN_STRING_MIN_LEN)


def dump_tokens(client: Garmin) -> str | None:
    """The client's session tokens as a JSON string; None if not storable.

    A session from garminconnect's cookie fallback has no tokens to
    serialize; storing it would only fail the next token login.
    """
    tokens = client.client.dumps()
    try:
        data = json.loads(tokens)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("di_token"):
        return None
    return tokens


def _login_from_tokens(tokens_json: str) -> Garmin:
    """Resume a session from saved tokens (loads profile and settings)."""
    client = Garmin()
    client.login(_pad_tokens(tokens_json))
    return client


def _credential_login(ctx: GarminSyncContext, password: str) -> Garmin:
    """Log in with the user's Garmin password.

    Raises MfaRequiredError (login parked in `ctx.pending`) when Garmin
    wants a one-time code, GarminLoginRequiredError when it rejects the
    credentials.
    """
    # With no tokenstore argument, garminconnect falls back to the
    # GARMINTOKENS environment variable — and would log in as whoever's
    # tokens that names instead of this user.
    if os.environ.get("GARMINTOKENS"):
        raise SyncError(
            "The server's environment sets GARMINTOKENS, which would replace "
            "this account's Garmin login; the server admin must unset it."
        )
    client = Garmin(email=ctx.username, password=password, return_on_mfa=True)
    try:
        mfa_status, _ = client.login()
    except GarminConnectAuthenticationError as e:
        raise GarminLoginRequiredError(
            "Garmin rejected the username or password."
        ) from e
    finally:
        # Only the login itself needs the password (an MFA resume uses the
        # state the client keeps), so don't let a parked client hold it.
        client.password = None
    if mfa_status == "needs_mfa":
        ctx.pending.put(client, ctx.username)
        raise MfaRequiredError("Garmin login needs the MFA code Garmin just sent you.")
    logger.info("Garmin credential login for user %d succeeded", ctx.user_id)
    tokens = dump_tokens(client)
    if tokens is None:
        return client
    # return_on_mfa logins skip loading the profile; resume from the new
    # tokens to get a fully initialized client.
    return _login_from_tokens(tokens)


def _authenticate(ctx: GarminSyncContext) -> Garmin:
    """Log in to Garmin for `ctx`: saved tokens first, then the password.

    Raises GarminLoginRequiredError when neither works (no tokens or they
    were rejected, and no password given), MfaRequiredError when the
    credential login waits for its code.
    """
    if ctx.tokens_json:
        try:
            client = _login_from_tokens(ctx.tokens_json)
        except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
            logger.info(
                "Saved Garmin session of user %d not usable (%s)",
                ctx.user_id,
                type(e).__name__,
            )
        else:
            logger.info("Logged in to Garmin from saved tokens (user %d)", ctx.user_id)
            return client
    if not ctx.password:
        raise GarminLoginRequiredError(
            "Garmin needs your password to log in again."
            if ctx.tokens_json
            else "Log in to Garmin to sync."
        )
    return _credential_login(ctx, ctx.password)


def complete_mfa(pending: PendingMfaSlot, code: str, username: str) -> str | None:
    """Finish the login parked in `pending` with `code`; return its tokens.

    `username` is the Garmin account the caller will store the tokens for;
    a login parked for any other account is dropped instead of resumed.
    The tokens (None if the session has none to store) are for the caller
    to seal and save; a following sync then logs in from them. On a wrong
    code the pending login is kept so the user can retry.

    Raises:
        SyncError: no pending login for `username` (never started,
            finished, expired, or for an account the user switched away from).
        GarminConnectAuthenticationError: Garmin rejected the code.
        GarminConnectTooManyRequestsError: Garmin rate-limited the verify.
    """
    entry = pending.get(MFA_PENDING_TTL_SEC)
    if entry is None:
        raise SyncError("No Garmin login is waiting for an MFA code. Sync again.")
    client, parked_for = entry
    if parked_for != username:
        # A login for the old account that was still in flight when the user
        # switched accounts, parked after the switch cleared the slot.
        pending.discard(client)
        raise SyncError("No Garmin login is waiting for an MFA code. Sync again.")
    client.resume_login({}, code.strip())
    pending.discard(client)
    logger.info("Garmin MFA login successful")
    return dump_tokens(client)


def _fetch_activities(
    client: Garmin, activity_limit: int, sport_filter: Iterable[str] = ()
) -> list[dict[str, object]]:
    """Fetch the newest `activity_limit` GPS activity summaries from Garmin Connect.

    Pages through the list: garminconnect caps one get_activities() call at
    ACTIVITY_PAGE_SIZE, and an activity_limit above that would otherwise
    silently drop the oldest activities from the map.
    """
    activities: list[dict[str, object]] = []
    while len(activities) < activity_limit:
        page_size = min(ACTIVITY_PAGE_SIZE, activity_limit - len(activities))
        result = client.get_activities(start=len(activities), limit=page_size)
        if not isinstance(result, list):
            logger.error("Unexpected response from get_activities")
            break
        activities.extend(result)
        if len(result) < page_size:
            break
    logger.info("Fetched %d activities", len(activities))

    # Filter to GPS-enabled activities
    gps_activities = [a for a in activities if a.get("startLatitude") is not None]
    logger.info("%d activities have GPS data", len(gps_activities))

    allowed = set(sport_filter)
    if allowed:
        gps_activities = [
            a
            for a in gps_activities
            if isinstance(a.get("activityType"), dict)
            and a["activityType"].get("typeKey") in allowed  # type: ignore[union-attr]
        ]
        logger.info("%d activities after sport filter", len(gps_activities))

    return gps_activities


def _download_gpx(client: Garmin, activity_id: str, gpx_path: Path) -> bool:
    """Download a GPX file for an activity. Returns True if downloaded."""
    if gpx_path.is_file():
        logger.debug("Skipping %s, already exists", gpx_path.name)
        return False

    try:
        data = client.download_activity(
            activity_id, dl_fmt=Garmin.ActivityDownloadFormat.GPX
        )
        if not isinstance(data, bytes) or len(data) < 100:
            logger.warning("GPX for activity %s is empty/too small, skipping", activity_id)
            return False
        gpx_path.write_bytes(data)
        logger.info("Downloaded %s", gpx_path.name)
        return True
    except GarminConnectConnectionError as e:
        logger.warning("Failed to download GPX for activity %s: %s", activity_id, e)
        return False


def _format_notes(activity: dict[str, object]) -> str:
    """Build a summary notes string from activity metadata."""
    parts: list[str] = []

    start_time = activity.get("startTimeLocal")
    if isinstance(start_time, str):
        parts.append(start_time.split(" ")[0] if " " in start_time else start_time)

    distance = activity.get("distance")
    if isinstance(distance, (int, float)) and distance > 0:
        km = distance / 1000
        parts.append(f"{km:.1f} km")

    duration = activity.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        hours = int(duration // 3600)
        minutes = int((duration % 3600) // 60)
        if hours > 0:
            parts.append(f"{hours}h {minutes:02d}m")
        else:
            parts.append(f"{minutes}m")

    return " | ".join(parts)


def activities_to_routes(
    activities: Iterable[Mapping[str, Any]], strava_db: Path | None = None
) -> list[RouteConfig]:
    """Turn Garmin activity summaries into the user's Garmin routes.

    If `strava_db` is given, each activity is matched against it; on a
    match, name and sport come from Strava and `strava_id` is recorded.
    """
    routes: list[RouteConfig] = []
    matched = 0
    for activity in activities:
        activity_id = str(activity["activityId"])
        name = str(activity.get("activityName") or f"Activity {activity_id}")

        activity_type = activity.get("activityType")
        type_key = ""
        if isinstance(activity_type, dict):
            type_key = str(activity_type.get("typeKey", ""))
        sport = _map_sport(type_key)
        strava_id: int | None = None

        if strava_db is not None:
            # Match on UTC: Strava archive imports only carry UTC start times.
            start_gmt = activity.get("startTimeGMT")
            distance = activity.get("distance")
            if isinstance(start_gmt, str) and isinstance(distance, (int, float)):
                match = strava.find_match(strava_db, start_gmt, float(distance))
                if match is not None:
                    name = match.name
                    sport = match.sport
                    strava_id = match.strava_id
                    matched += 1

        routes.append(
            RouteConfig(
                id=f"{ROUTE_ID_PREFIX}{activity_id}",
                name=name,
                sport=[sport],
                status=Status.DONE,
                notes=_format_notes(dict(activity)),
                gpx=(GPX_SUBDIR / f"{activity_id}.gpx").as_posix(),
                strava_id=strava_id,
            )
        )
    if strava_db is not None:
        logger.info("Strava-matched %d/%d activities", matched, len(routes))
    return routes


def _write_activity_index(user_dir: Path, activities: list[dict[str, object]]) -> None:
    """Keep the activity summaries of this sync (atomically replaced)."""
    path = user_dir / ACTIVITY_INDEX
    slim = [{k: a[k] for k in _INDEX_FIELDS if k in a} for a in activities]
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(slim))
    os.replace(tmp, path)


def load_activity_index(user_dir: Path) -> list[dict[str, Any]] | None:
    """The activity summaries of the user's last Garmin sync, or None if none."""
    path = user_dir / ACTIVITY_INDEX
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable Garmin activity index %s: %s", path, e)
        return None
    if not isinstance(data, list):
        return None
    return [a for a in data if isinstance(a, dict) and "activityId" in a]


def rematch_strava(
    db_path: Path, user_id: int, user_dir: Path, strava_db: Path
) -> int | None:
    """Re-derive the user's Garmin routes against their (new) Strava DB.

    Rebuilds the routes from the last sync's activity summaries — exactly
    what that sync would write with this Strava DB — and returns how many
    are matched to a Strava activity. Returns None, changing nothing, when
    no Garmin sync has run for this account yet.
    """
    activities = load_activity_index(user_dir)
    if activities is None:
        return None
    routes = activities_to_routes(activities, strava_db)
    routes_store.replace_source(db_path, user_id, "garmin", routes)
    return sum(route.strava_id is not None for route in routes)


def _save_tokens(ctx: GarminSyncContext, client: Garmin, last: str | None) -> str | None:
    """Hand the client's tokens to `ctx.on_tokens` if they changed since `last`."""
    tokens = dump_tokens(client)
    if tokens is None:
        logger.warning(
            "Garmin session of user %d has no storable tokens; the next sync "
            "will ask for the password again",
            ctx.user_id,
        )
    elif tokens != last:
        ctx.on_tokens(tokens)
    return tokens


def sync(ctx: GarminSyncContext) -> None:
    """Sync the user's Garmin activities into GPX files and their routes.

    Raises GarminLoginRequiredError when Garmin needs the password,
    MfaRequiredError when the login is parked in `ctx.pending` awaiting its
    code, and SyncError on other authentication failures or rate-limiting,
    each with a user-readable message.
    """
    gpx_dir = ctx.user_dir / GPX_SUBDIR
    gpx_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = _authenticate(ctx)
    except GarminConnectTooManyRequestsError as e:
        raise SyncError(
            f"Garmin rate-limited: {e}. "
            "Wait 15-60 minutes before retrying, or try a different network."
        ) from e
    except GarminConnectAuthenticationError as e:
        raise SyncError(f"Garmin authentication failed: {e}") from e
    tokens = _save_tokens(ctx, client, None)

    activities = _fetch_activities(client, ctx.activity_limit, ctx.sport_filter)

    downloaded_count = 0
    for activity in activities:
        activity_id = str(activity["activityId"])
        gpx_path = gpx_dir / f"{activity_id}.gpx"
        if _download_gpx(client, activity_id, gpx_path):
            downloaded_count += 1
            time.sleep(1)

    logger.info(
        "Downloaded %d new GPX files (%d total activities)",
        downloaded_count,
        len(activities),
    )

    strava_db = ctx.strava_db if ctx.strava_db and ctx.strava_db.is_file() else None
    routes = activities_to_routes(activities, strava_db)
    _write_activity_index(ctx.user_dir, activities)
    routes_store.replace_source(ctx.db_path, ctx.user_id, "garmin", routes)
    logger.info("Stored %d Garmin routes for user %d", len(routes), ctx.user_id)
    # The client refreshes its tokens when they near expiry; keep the newest.
    _save_tokens(ctx, client, tokens)
