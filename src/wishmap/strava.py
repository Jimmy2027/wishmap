"""Sync activity metadata from Strava into a SQLite DB and match against Garmin."""

import json
import logging
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from wishmap.models import StravaConfig

logger = logging.getLogger("wishmap.strava")

OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"
OAUTH_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
DEFAULT_REDIRECT_URI = "http://localhost"
DEFAULT_SCOPE = "activity:read_all"
HTTP_TIMEOUT_SEC = 30

# Strava sport_type -> (wishmap_sport, hex_color). Colors mirror garmin.SPORT_MAP.
STRAVA_SPORT_MAP: dict[str, tuple[str, str]] = {
    "Hike": ("hiking", "#4a7c59"),
    "Run": ("running", "#E74C3C"),
    "TrailRun": ("trail_running", "#c0392b"),
    "VirtualRun": ("running", "#E74C3C"),
    "Ride": ("biking", "#3498DB"),
    "VirtualRide": ("biking", "#3498DB"),
    "MountainBikeRide": ("mountain_biking", "#8E44AD"),
    "EMountainBikeRide": ("mountain_biking", "#8E44AD"),
    "GravelRide": ("gravel", "#E67E22"),
    "EBikeRide": ("biking", "#2E86DE"),
    "Walk": ("walking", "#7F8C8D"),
    "BackcountrySki": ("ski_touring", "#E74C3C"),
    "AlpineSki": ("skiing", "#3498DB"),
    "NordicSki": ("cross_country_skiing", "#1ABC9C"),
    "Snowshoe": ("snowshoeing", "#9B59B6"),
    "Snowboard": ("skiing", "#3498DB"),
    "Swim": ("swimming", "#2980B9"),
    "StandUpPaddling": ("sup", "#1ABC9C"),
    "RockClimbing": ("climbing", "#E67E22"),
    "Kayaking": ("kayaking", "#16A085"),
    "Canoeing": ("kayaking", "#16A085"),
    "Rowing": ("rowing", "#16A085"),
}

@dataclass
class StravaMatch:
    strava_id: int
    name: str
    sport: str
    color: str | None  # None when Strava's sport_type isn't in STRAVA_SPORT_MAP


def _map_sport(sport_type: str) -> tuple[str, str | None]:
    """Return (wishmap_sport, color). Color is None for unmapped sport_types
    so the caller can preserve its existing color rather than substituting grey."""
    if sport_type in STRAVA_SPORT_MAP:
        return STRAVA_SPORT_MAP[sport_type]
    return (sport_type.lower(), None)


def _get_client_secret(cfg: StravaConfig) -> str:
    if cfg.client_secret:
        return cfg.client_secret
    if cfg.client_secret_file:
        return Path(cfg.client_secret_file).expanduser().read_text().strip()
    if cfg.client_secret_pass:
        result = subprocess.run(
            ["pass", "show", cfg.client_secret_pass],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.splitlines()[0].strip()
    raise ValueError(
        "Strava config needs one of 'client_secret', 'client_secret_file', "
        "or 'client_secret_pass'"
    )


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        body = resp.read().decode()
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Unexpected token response: {body[:200]}")
    return parsed


def _get_json(url: str, access_token: str) -> Any:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode())


def _load_tokens(tokenstore: Path) -> dict[str, Any] | None:
    if not tokenstore.is_file():
        return None
    return json.loads(tokenstore.read_text())


def _save_tokens(tokenstore: Path, tokens: dict[str, Any]) -> None:
    tokenstore.parent.mkdir(parents=True, exist_ok=True)
    tokenstore.write_text(json.dumps(tokens, indent=2))
    os.chmod(tokenstore, 0o600)


def _refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str
) -> dict[str, Any]:
    return _post_form(
        OAUTH_TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )


def _ensure_access_token(cfg: StravaConfig) -> str:
    tokenstore = Path(cfg.tokenstore).expanduser()
    tokens = _load_tokens(tokenstore)
    if tokens is None:
        raise SystemExit(
            f"No Strava tokens at {tokenstore}. Run 'wishmap --strava-auth' first."
        )

    expires_at = int(tokens.get("expires_at", 0))
    if expires_at - int(time.time()) > 60:
        return str(tokens["access_token"])

    logger.info("Refreshing Strava access token")
    client_secret = _get_client_secret(cfg)
    fresh = _refresh_access_token(cfg.client_id, client_secret, tokens["refresh_token"])
    _save_tokens(tokenstore, fresh)
    return str(fresh["access_token"])


def authorize(cfg: StravaConfig) -> None:
    """Interactive one-time OAuth setup. Prints URL, reads pasted code, saves tokens."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    params = urllib.parse.urlencode(
        {
            "client_id": cfg.client_id,
            "response_type": "code",
            "redirect_uri": DEFAULT_REDIRECT_URI,
            "approval_prompt": "auto",
            "scope": DEFAULT_SCOPE,
        }
    )
    auth_url = f"{OAUTH_AUTHORIZE_URL}?{params}"

    print("Open this URL in your browser and authorize wishmap:")
    print()
    print(f"  {auth_url}")
    print()
    print("Strava will redirect you to a localhost URL that will fail to load.")
    print("Copy the 'code' query parameter from the address bar and paste it here.")
    print()
    code = input("code: ").strip()
    if not code:
        raise SystemExit("No code provided")

    client_secret = _get_client_secret(cfg)
    tokens = _post_form(
        OAUTH_TOKEN_URL,
        {
            "client_id": cfg.client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    if "refresh_token" not in tokens:
        raise SystemExit(f"Token exchange failed: {tokens}")

    tokenstore = Path(cfg.tokenstore).expanduser()
    _save_tokens(tokenstore, tokens)
    logger.info("Saved Strava tokens to %s", tokenstore)


def _fetch_activities(access_token: str, limit: int) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    page = 1
    per_page = 200
    while len(activities) < limit:
        params = urllib.parse.urlencode({"per_page": per_page, "page": page})
        try:
            batch = _get_json(f"{ACTIVITIES_URL}?{params}", access_token)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Strava API error on page {page}: {e}") from e
        if not isinstance(batch, list) or not batch:
            break
        activities.extend(batch)
        logger.info("Fetched page %d (%d activities so far)", page, len(activities))
        page += 1
        time.sleep(0.5)

    activities = activities[:limit]
    gps = [a for a in activities if a.get("start_latlng")]
    logger.info("%d total fetched, %d have GPS", len(activities), len(gps))
    return gps


def _db_path(cfg: StravaConfig, base_path: Path) -> Path:
    return base_path / cfg.gpx_dir / "activities.db"


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sport_type TEXT NOT NULL,
            type TEXT NOT NULL,
            start_date TEXT NOT NULL,
            start_date_local TEXT NOT NULL,
            distance REAL NOT NULL,
            moving_time INTEGER NOT NULL,
            elapsed_time INTEGER NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_start_local ON activities(start_date_local);
        """
    )


def _upsert_activity(conn: sqlite3.Connection, a: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO activities
        (id, name, sport_type, type, start_date, start_date_local,
         distance, moving_time, elapsed_time, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(a["id"]),
            str(a.get("name", "")),
            str(a.get("sport_type", a.get("type", ""))),
            str(a.get("type", "")),
            str(a.get("start_date", "")),
            str(a.get("start_date_local", "")),
            float(a.get("distance") or 0.0),
            int(a.get("moving_time") or 0),
            int(a.get("elapsed_time") or 0),
            json.dumps(a),
        ),
    )


def _parse_garmin_local(s: str) -> datetime | None:
    """Parse Garmin's startTimeLocal ('YYYY-MM-DD HH:MM:SS')."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _parse_strava_local(s: str) -> datetime | None:
    """Parse Strava's start_date_local ('YYYY-MM-DDTHH:MM:SSZ', naive local)."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def find_match(
    db_path: Path,
    garmin_start_local: str,
    garmin_distance_m: float,
    time_tol_sec: int = 300,
    dist_tol_pct: float = 0.05,
) -> StravaMatch | None:
    """Look up a Strava activity matching the given Garmin activity.

    Returns the closest-by-time candidate within ±time_tol_sec whose distance
    is within ±dist_tol_pct of garmin_distance_m. Returns None if db_path is
    missing or no candidate matches.
    """
    if not db_path.is_file():
        return None
    g_dt = _parse_garmin_local(garmin_start_local)
    if g_dt is None:
        return None

    lo = (g_dt - timedelta(seconds=time_tol_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (g_dt + timedelta(seconds=time_tol_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, sport_type, start_date_local, distance "
            "FROM activities WHERE start_date_local BETWEEN ? AND ?",
            (lo, hi),
        ).fetchall()
    finally:
        conn.close()

    best: tuple[int, int, str, str] | None = None  # (time_delta_sec, id, name, sport_type)
    for row in rows:
        sid, name, sport_type, start_local, distance = row
        s_dt = _parse_strava_local(start_local)
        if s_dt is None:
            continue
        if garmin_distance_m > 0 and distance > 0:
            if abs(distance - garmin_distance_m) / garmin_distance_m > dist_tol_pct:
                continue
        delta = abs(int((s_dt - g_dt).total_seconds()))
        if best is None or delta < best[0]:
            best = (delta, sid, name, sport_type)

    if best is None:
        return None
    _, sid, name, sport_type = best
    sport, color = _map_sport(sport_type)
    return StravaMatch(strava_id=sid, name=name, sport=sport, color=color)


def sync(cfg: StravaConfig, base_path: Path) -> None:
    """Fetch Strava activities and upsert into the local SQLite DB."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    db_file = _db_path(cfg, base_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    access_token = _ensure_access_token(cfg)
    activities = _fetch_activities(access_token, cfg.activity_limit)

    if cfg.sport_filter:
        allowed = set(cfg.sport_filter)
        activities = [a for a in activities if a.get("sport_type") in allowed]
        logger.info("%d activities after sport filter", len(activities))

    conn = sqlite3.connect(db_file)
    try:
        _init_db(conn)
        for a in activities:
            _upsert_activity(conn, a)
        conn.commit()
    finally:
        conn.close()

    logger.info("Upserted %d Strava activities into %s", len(activities), db_file)
