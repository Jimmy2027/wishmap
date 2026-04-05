import argparse
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

from wishmap.config import load_config, resolve_config_path
from wishmap.geojson import pins_to_geojson, routes_to_geojson
from wishmap.models import WishmapConfig

_config: WishmapConfig
_pins_geojson: dict[str, Any]
_routes_geojson: dict[str, Any]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    global _config, _pins_geojson, _routes_geojson
    config_path = resolve_config_path()
    _config = load_config(config_path)
    base_path = config_path.parent
    _pins_geojson = pins_to_geojson(_config.pins)
    _routes_geojson = routes_to_geojson(_config.routes, base_path)
    yield


app = FastAPI(lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config() -> dict[str, str]:
    return {"title": _config.title}


@app.get("/api/pins")
async def get_pins() -> dict[str, Any]:
    return _pins_geojson


@app.get("/api/routes")
async def get_routes() -> dict[str, Any]:
    return _routes_geojson


def main() -> None:
    parser = argparse.ArgumentParser(description="Wishmap server")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.config:
        os.environ["WISHMAP_CONFIG"] = args.config
    uvicorn.run("wishmap.app:app", host=args.host, port=args.port, reload=True)
