# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                          # install dependencies
uv run wishmap                   # run dev server at http://127.0.0.1:8000
uv run wishmap --host 0.0.0.0 --port 3000  # custom host/port
uv run wishmap --config /path/to/config.toml  # custom config
uv run wishmap --sync            # sync Garmin activities, then exit
uv run pyright src/              # type-check (also runs as pre-commit hook)
```

## Architecture

Wishmap is a self-hosted map app for outdoor sports (hiking, biking, ski touring). It displays pins and GPX routes on a Leaflet/OpenStreetMap basemap, configured entirely via a TOML file (`wishmap.toml`).

**Backend** (FastAPI + uvicorn, Python 3.13):
- `src/wishmap/app.py` — FastAPI app with lifespan startup. Serves a single-page HTML frontend and three JSON API endpoints (`/api/config`, `/api/pins`, `/api/routes`). Config path resolved via `WISHMAP_CONFIG` env var or defaults to `wishmap.toml` in cwd.
- `src/wishmap/models.py` — Pydantic models (`WishmapConfig`, `PinConfig`, `RouteConfig`, `GarminConfig`). `sport` field accepts string or list via a field validator.
- `src/wishmap/config.py` — Loads and validates the TOML config, checks that referenced GPX files exist. Supports `includes` for merging multiple TOML files.
- `src/wishmap/garmin.py` — Syncs Garmin Connect activities: authenticates via `garth`, downloads GPX files to `data/garmin/`, generates `garmin.toml` with route entries.
- `src/wishmap/gpx.py` — Parses GPX files into `[lon, lat]` coordinate lists using `gpxpy`.
- `src/wishmap/geojson.py` — Converts pins and routes to GeoJSON FeatureCollections. Also generates "route start" point features from the first coordinate of each GPX track.

**Frontend** (single file `src/wishmap/static/index.html`):
- Vanilla JS with Leaflet. Fetches GeoJSON from the API, renders pins as circle markers and routes as polylines. Filter panel for sport and status. All CSS is inline.

**Data flow**: TOML config -> Pydantic models -> GeoJSON dicts (built once at startup) -> served via API -> rendered by Leaflet in the browser.
