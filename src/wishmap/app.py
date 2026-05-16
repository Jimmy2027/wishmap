import argparse
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from wishmap import ratings
from wishmap.config import load_config, resolve_config_path
from wishmap.geojson import pins_to_geojson, route_start_pins_to_geojson, routes_to_geojson
from wishmap.models import (
    ConfigResponse,
    FeatureCollection,
    RouteRating,
    RouteRatingIn,
    WishmapConfig,
)

_config: WishmapConfig
_pins_geojson: FeatureCollection
_routes_geojson: FeatureCollection
_db_path: Path


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    global _config, _pins_geojson, _routes_geojson, _db_path
    config_path = resolve_config_path()
    _config = load_config(config_path)
    base_path = config_path.parent
    _db_path = base_path / "data" / "wishmap.db"
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _pins_geojson = pins_to_geojson(_config.pins)
    _pins_geojson.features.extend(route_start_pins_to_geojson(_config.routes, base_path))
    _routes_geojson = routes_to_geojson(_config.routes, base_path)
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

    uvicorn.run("wishmap.app:app", host=args.host, port=args.port, reload=True)
