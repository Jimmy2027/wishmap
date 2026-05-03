"""Sync activities from Garmin Connect."""

import json
import logging
import subprocess
import time
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from wishmap import strava
from wishmap.models import GarminConfig

logger = logging.getLogger("wishmap.garmin")

# Garmin activityType.typeKey -> (wishmap_sport, hex_color)
SPORT_MAP: dict[str, tuple[str, str]] = {
    "hiking": ("hiking", "#4a7c59"),
    "running": ("running", "#E74C3C"),
    "trail_running": ("trail_running", "#c0392b"),
    "cycling": ("biking", "#2E86DE"),
    "mountain_biking": ("mountain_biking", "#8E44AD"),
    "gravel_cycling": ("gravel", "#E67E22"),
    "road_biking": ("biking", "#3498DB"),
    "walking": ("walking", "#7F8C8D"),
    "mountaineering": ("mountaineering", "#2C3E50"),
    "backcountry_skiing_snowboarding": ("ski_touring", "#E74C3C"),
    "resort_skiing_snowboarding": ("skiing", "#3498DB"),
    "cross_country_skiing": ("cross_country_skiing", "#1ABC9C"),
    "snowshoeing": ("snowshoeing", "#9B59B6"),
    "swimming": ("swimming", "#2980B9"),
    "open_water_swimming": ("swimming", "#2471A3"),
    "stand_up_paddleboarding": ("sup", "#1ABC9C"),
    "rock_climbing": ("climbing", "#E67E22"),
    "kayaking": ("kayaking", "#16A085"),
}

DEFAULT_COLOR = "#95A5A6"


def _map_sport(type_key: str) -> tuple[str, str]:
    """Return (wishmap_sport, color) for a Garmin activity type key."""
    return SPORT_MAP.get(type_key, (type_key, DEFAULT_COLOR))


def _get_password(garmin_config: GarminConfig) -> str:
    """Resolve the password from config (direct, file, or passwordstore)."""
    if garmin_config.password:
        return garmin_config.password
    if garmin_config.password_file:
        path = Path(garmin_config.password_file).expanduser()
        return path.read_text().strip()
    if garmin_config.password_pass:
        result = subprocess.run(
            ["pass", "show", garmin_config.password_pass],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.splitlines()[0].strip()
    raise ValueError(
        "Garmin config needs one of 'password', 'password_file', or 'password_pass'"
    )


def _authenticate(garmin_config: GarminConfig) -> Garmin:
    """Authenticate with Garmin Connect, reusing saved tokens when possible."""
    tokenstore = str(Path(garmin_config.tokenstore).expanduser())

    # Try resuming saved tokens first — no network login required
    try:
        client = Garmin()
        client.login(tokenstore)
        logger.info("Logged in using saved tokens from %s", tokenstore)
        return client
    except (GarminConnectAuthenticationError, GarminConnectConnectionError):
        logger.info("No valid tokens, performing fresh login")

    # Fresh login with credentials
    password = _get_password(garmin_config)
    client = Garmin(email=garmin_config.username, password=password)
    client.login(tokenstore)
    logger.info("Fresh login successful, tokens saved to %s", tokenstore)
    return client


def _fetch_activities(
    client: Garmin, garmin_config: GarminConfig
) -> list[dict[str, object]]:
    """Fetch activity summaries from Garmin Connect."""
    result = client.get_activities(start=0, limit=garmin_config.activity_limit)
    if not isinstance(result, list):
        logger.error("Unexpected response from get_activities")
        return []
    activities: list[dict[str, object]] = result
    logger.info("Fetched %d activities", len(activities))

    # Filter to GPS-enabled activities
    gps_activities = [a for a in activities if a.get("startLatitude") is not None]
    logger.info("%d activities have GPS data", len(gps_activities))

    if garmin_config.sport_filter:
        allowed = set(garmin_config.sport_filter)
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


def _toml_quote(s: str) -> str:
    """Return s as a quoted TOML basic string. Handles control chars, quotes, backslashes."""
    return json.dumps(s, ensure_ascii=False)


def _generate_toml(
    activities: list[dict[str, object]],
    gpx_dir: str,
    strava_db: Path | None = None,
) -> str:
    """Generate TOML content with [[routes]] entries for all activities.

    If strava_db is given, each Garmin activity is matched against the Strava
    DB; on a match, name/sport/color are overridden and 'strava' is added to tags.
    """
    lines = ["# Auto-generated by wishmap garmin sync", ""]
    matched = 0

    for activity in activities:
        activity_id = str(activity["activityId"])
        name = str(activity.get("activityName", f"Activity {activity_id}"))

        activity_type = activity.get("activityType")
        type_key = ""
        if isinstance(activity_type, dict):
            type_key = str(activity_type.get("typeKey", ""))

        sport, color = _map_sport(type_key)
        notes = _format_notes(activity)
        gpx_rel = f"{gpx_dir}/{activity_id}.gpx"
        tags = ["garmin"]

        if strava_db is not None:
            start_local = activity.get("startTimeLocal")
            distance = activity.get("distance")
            if isinstance(start_local, str) and isinstance(distance, (int, float)):
                match = strava.find_match(strava_db, start_local, float(distance))
                if match is not None:
                    name = match.name
                    sport = match.sport
                    if match.color is not None:
                        color = match.color
                    tags.append("strava")
                    matched += 1

        tags_toml = ", ".join(f'"{t}"' for t in tags)

        lines.append("[[routes]]")
        lines.append(f'id = "garmin-{activity_id}"')
        lines.append(f"name = {_toml_quote(name)}")
        lines.append(f'sport = "{sport}"')
        lines.append('status = "done"')
        lines.append(f"tags = [{tags_toml}]")
        lines.append(f"notes = {_toml_quote(notes)}")
        lines.append(f'color = "{color}"')
        lines.append(f'gpx = "{gpx_rel}"')
        lines.append("")

    if strava_db is not None:
        logger.info("Strava-matched %d/%d activities", matched, len(activities))

    return "\n".join(lines)


def sync(
    garmin_config: GarminConfig,
    base_path: Path,
    strava_db: Path | None = None,
) -> None:
    """Sync Garmin Connect activities to local GPX files and generate TOML."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    gpx_dir = base_path / garmin_config.gpx_dir
    gpx_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = _authenticate(garmin_config)
    except GarminConnectTooManyRequestsError as e:
        raise SystemExit(
            f"Garmin rate-limited: {e}\n"
            "Wait 15-60 minutes before retrying, or try a different network."
        ) from e
    except GarminConnectAuthenticationError as e:
        raise SystemExit(f"Garmin authentication failed: {e}") from e

    activities = _fetch_activities(client, garmin_config)

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

    toml_content = _generate_toml(activities, garmin_config.gpx_dir, strava_db)
    toml_path = gpx_dir / "garmin.toml"
    toml_path.write_text(toml_content)
    logger.info("Generated %s with %d routes", toml_path, len(activities))
