import argparse
import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from wishmap import ratings, secrets, sync_runner
from wishmap.config import load_config, resolve_config_path
from wishmap.exceptions import (
    AgentUnreachableError,
    BadPassphraseError,
    LoopbackDisabledError,
    SyncError,
)
from wishmap.geojson import pins_to_geojson, route_start_pins_to_geojson, routes_to_geojson
from wishmap.models import (
    ConfigResponse,
    FeatureCollection,
    RouteRating,
    RouteRatingIn,
    WishmapConfig,
)


class UnlockRequest(BaseModel):
    passphrase: str

_config: WishmapConfig
_config_path: Path
_base_path: Path
_pins_geojson: FeatureCollection
_routes_geojson: FeatureCollection
_db_path: Path


def _build_data(
    config: WishmapConfig, base_path: Path
) -> tuple[FeatureCollection, FeatureCollection]:
    """Build the pins and routes GeoJSON collections from a loaded config.

    Shared by lifespan startup and reload_data() so the two stay in lockstep.
    """
    pins_fc = pins_to_geojson(config.pins)
    pins_fc.features.extend(route_start_pins_to_geojson(config.routes, base_path))
    routes_fc = routes_to_geojson(config.routes, base_path)
    return pins_fc, routes_fc


def reload_data() -> None:
    """Re-read the config and rebuild the in-memory GeoJSON collections.

    Called by sync_runner after a successful sync so the running server
    picks up new GPX files and garmin.toml entries without a restart.
    Wraps any failure in SyncError so a corrupt config or missing GPX
    file surfaces a clean error instead of killing the worker.
    """
    global _config, _pins_geojson, _routes_geojson
    try:
        config = load_config(_config_path)
        pins_fc, routes_fc = _build_data(config, _base_path)
    except SystemExit as e:
        # config.load_config calls sys.exit(1) on missing GPX. Convert
        # to SyncError so the sync task records it instead of exiting.
        raise SyncError(f"Config reload failed: {e}") from e
    except Exception as e:
        raise SyncError(f"Config reload failed: {e}") from e
    _config = config
    _pins_geojson = pins_fc
    _routes_geojson = routes_fc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    global _config, _config_path, _base_path, _pins_geojson, _routes_geojson, _db_path
    _config_path = resolve_config_path()
    _config = load_config(_config_path)
    _base_path = _config_path.parent
    _db_path = _base_path / "data" / "wishmap.db"
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _pins_geojson, _routes_geojson = _build_data(_config, _base_path)
    yield


app = FastAPI(lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config() -> ConfigResponse:
    return ConfigResponse(title=_config.title)


@app.get("/api/pins")
async def get_pins() -> FeatureCollection:
    return _pins_geojson


@app.get("/api/routes")
async def get_routes() -> FeatureCollection:
    return _routes_geojson


# Sync handlers — FastAPI dispatches `def` handlers to a threadpool, so the
# blocking sqlite3 calls don't stall the event loop.
@app.get("/api/ratings")
def get_ratings() -> dict[str, RouteRating]:
    return {
        route_id: RouteRating(**row)
        for route_id, row in ratings.get_all(_db_path).items()
    }


@app.put("/api/ratings/{route_id}")
def put_rating(route_id: str, patch: RouteRatingIn) -> RouteRating:
    if route_id not in {r.id for r in _config.routes}:
        raise HTTPException(status_code=404, detail="unknown route_id")
    row = ratings.upsert(
        _db_path, route_id, patch.model_dump(exclude_unset=True)
    )
    return RouteRating(**row)


@app.post("/api/sync")
async def post_sync() -> JSONResponse:
    """Start a background sync of all configured services.

    Returns 202 with current status when a new sync starts; 409 with the
    current status when one is already running. Idempotent under spam:
    repeated clicks while running just keep returning the same status.
    """
    if not sync_runner.begin():
        return JSONResponse(status_code=409, content=sync_runner.get_status())
    asyncio.create_task(sync_runner.run_sync(_config, _base_path))
    return JSONResponse(status_code=202, content=sync_runner.get_status())


@app.get("/api/sync/status")
async def get_sync_status() -> dict[str, object]:
    return sync_runner.get_status()


def _warm_entries(config: WishmapConfig) -> list[str]:
    """Build the deduplicated list of pass entries to warm in /api/unlock.

    Precedence (deterministic, design A3):
      1. config.warm_pass_entry — smoke-test entry first
      2. strava.client_secret_pass
      3. garmin.password_pass

    Duplicates are dropped while preserving order. Empty strings dropped.
    """
    candidates: list[str] = []
    if config.warm_pass_entry:
        candidates.append(config.warm_pass_entry)
    if config.strava is not None and config.strava.client_secret_pass:
        candidates.append(config.strava.client_secret_pass)
    if config.garmin is not None and config.garmin.password_pass:
        candidates.append(config.garmin.password_pass)
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


@app.post("/api/unlock")
async def post_unlock(req: UnlockRequest, request: Request) -> Response:
    """Warm gpg-agent against every configured pass entry.

    On success (all entries warmed), subsequent `pass show` calls during
    `default-cache-ttl` succeed without prompting — the web UI can then
    POST /api/sync and reach completion.

    Single-user self-hosted: knowing the GPG passphrase is the auth model.
    Sec-Fetch-Site is checked defensively against cross-origin posts from
    other tabs.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        # "none" covers direct navigation / curl without the header;
        # cross-site / same-site / cross-origin requests are rejected.
        return JSONResponse(
            status_code=403, content={"reason": "cross_origin_blocked"}
        )

    entries = _warm_entries(_config)
    if not entries:
        return JSONResponse(
            status_code=400, content={"reason": "no_pass_entries_configured"}
        )

    for entry in entries:
        try:
            await asyncio.to_thread(
                secrets.warm_gpg_agent, entry, req.passphrase
            )
        except BadPassphraseError:
            return JSONResponse(
                status_code=401,
                content={"reason": "bad_passphrase", "entry": entry},
            )
        except LoopbackDisabledError as e:
            return JSONResponse(
                status_code=503,
                content={"reason": "loopback_disabled", "hint": str(e)},
            )
        except AgentUnreachableError as e:
            return JSONResponse(
                status_code=503,
                content={"reason": "agent_unreachable", "hint": str(e)},
            )
        except SyncError as e:
            return JSONResponse(
                status_code=500,
                content={"reason": "unknown", "detail": str(e)},
            )
    return Response(status_code=204)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wishmap server")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--sync", action="store_true", help="Sync Garmin activities, then exit"
    )
    parser.add_argument(
        "--sync-strava",
        action="store_true",
        help="Sync Strava activities into the local DB, then exit",
    )
    parser.add_argument(
        "--strava-auth",
        action="store_true",
        help="Run one-time Strava OAuth setup, then exit",
    )
    args = parser.parse_args()
    if args.config:
        os.environ["WISHMAP_CONFIG"] = args.config

    # CLI modes get logging config here. Server mode inherits uvicorn's
    # logging setup; sync modules no longer call basicConfig themselves.
    if args.strava_auth or args.sync_strava or args.sync:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        if args.strava_auth:
            from wishmap import strava

            config_path = resolve_config_path()
            config = load_config(config_path)
            if config.strava is None:
                print("No [strava] section in config")
                return
            strava.authorize(config.strava)
            return

        if args.sync_strava:
            from wishmap import strava

            config_path = resolve_config_path()
            config = load_config(config_path)
            if config.strava is None:
                print("No [strava] section in config, nothing to sync")
                return
            strava.sync(config.strava, config_path.parent)
            return

        if args.sync:
            from wishmap.garmin import sync

            config_path = resolve_config_path()
            config = load_config(config_path)
            if config.garmin is None:
                print("No [garmin] section in config, nothing to sync")
                return
            strava_db = None
            if config.strava is not None:
                strava_db = config_path.parent / config.strava.gpx_dir / "activities.db"
            sync(config.garmin, config_path.parent, strava_db)
            return
    except SyncError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    uvicorn.run("wishmap.app:app", host=args.host, port=args.port, reload=True)
